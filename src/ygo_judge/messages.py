"""Decoder for the engine's binary messages. Layouts follow the writers in vendor/ygopro-core
(`new_message(MSG_...)` in playerop.cpp, processor.cpp, operations.cpp, field.cpp, card.cpp, libduel.cpp)."""

import struct
from dataclasses import dataclass, field
from typing import Any

MSG = {
    "RETRY": 1,
    "HINT": 2,
    "WIN": 5,
    "SELECT_BATTLECMD": 10,
    "SELECT_IDLECMD": 11,
    "SELECT_EFFECTYN": 12,
    "SELECT_YESNO": 13,
    "SELECT_OPTION": 14,
    "SELECT_CARD": 15,
    "SELECT_CHAIN": 16,
    "SELECT_PLACE": 18,
    "SELECT_POSITION": 19,
    "SELECT_TRIBUTE": 20,
    "SORT_CHAIN": 21,
    "SELECT_COUNTER": 22,
    "SELECT_SUM": 23,
    "SELECT_DISFIELD": 24,
    "SORT_CARD": 25,
    "SELECT_UNSELECT_CARD": 26,
    "CONFIRM_DECKTOP": 30,
    "CONFIRM_CARDS": 31,
    "SHUFFLE_DECK": 32,
    "SHUFFLE_HAND": 33,
    "REFRESH_DECK": 34,
    "SWAP_GRAVE_DECK": 35,
    "SHUFFLE_SET_CARD": 36,
    "REVERSE_DECK": 37,
    "DECK_TOP": 38,
    "SHUFFLE_EXTRA": 39,
    "NEW_TURN": 40,
    "NEW_PHASE": 41,
    "CONFIRM_EXTRATOP": 42,
    "MOVE": 50,
    "POS_CHANGE": 53,
    "SET": 54,
    "SWAP": 55,
    "FIELD_DISABLED": 56,
    "SUMMONING": 60,
    "SUMMONED": 61,
    "SPSUMMONING": 62,
    "SPSUMMONED": 63,
    "FLIPSUMMONING": 64,
    "FLIPSUMMONED": 65,
    "CHAINING": 70,
    "CHAINED": 71,
    "CHAIN_SOLVING": 72,
    "CHAIN_SOLVED": 73,
    "CHAIN_END": 74,
    "CHAIN_NEGATED": 75,
    "CHAIN_DISABLED": 76,
    "CARD_SELECTED": 80,
    "RANDOM_SELECTED": 81,
    "BECOME_TARGET": 83,
    "DRAW": 90,
    "DAMAGE": 91,
    "RECOVER": 92,
    "EQUIP": 93,
    "LPUPDATE": 94,
    "UNEQUIP": 95,
    "CARD_TARGET": 96,
    "CANCEL_TARGET": 97,
    "PAY_LPCOST": 100,
    "ADD_COUNTER": 101,
    "REMOVE_COUNTER": 102,
    "ATTACK": 110,
    "BATTLE": 111,
    "ATTACK_DISABLED": 112,
    "DAMAGE_STEP_START": 113,
    "DAMAGE_STEP_END": 114,
    "MISSED_EFFECT": 120,
    "BE_CHAIN_TARGET": 121,
    "CREATE_RELATION": 122,
    "RELEASE_RELATION": 123,
    "TOSS_COIN": 130,
    "TOSS_DICE": 131,
    "ROCK_PAPER_SCISSORS": 132,
    "HAND_RES": 133,
    "ANNOUNCE_RACE": 140,
    "ANNOUNCE_ATTRIB": 141,
    "ANNOUNCE_CARD": 142,
    "ANNOUNCE_NUMBER": 143,
    "CARD_HINT": 160,
    "TAG_SWAP": 161,
    "RELOAD_FIELD": 162,
    "AI_NAME": 163,
    "SHOW_HINT": 164,
    "PLAYER_HINT": 165,
    "MATCH_KILL": 170,
    "CUSTOM_MSG": 180,
    "REMOVE_CARDS": 190,
}
NAMES = {v: k for k, v in MSG.items()}
PROMPTS = {
    "SELECT_BATTLECMD",
    "SELECT_IDLECMD",
    "SELECT_EFFECTYN",
    "SELECT_YESNO",
    "SELECT_OPTION",
    "SELECT_CARD",
    "SELECT_CHAIN",
    "SELECT_PLACE",
    "SELECT_POSITION",
    "SELECT_TRIBUTE",
    "SORT_CHAIN",
    "SELECT_COUNTER",
    "SELECT_SUM",
    "SELECT_DISFIELD",
    "SORT_CARD",
    "SELECT_UNSELECT_CARD",
    "ROCK_PAPER_SCISSORS",
    "ANNOUNCE_RACE",
    "ANNOUNCE_ATTRIB",
    "ANNOUNCE_CARD",
    "ANNOUNCE_NUMBER",
}


@dataclass(frozen=True)
class Loc:
    """Where a card is: controller, location, sequence and position (for Xyz materials,
    `location` has LOCATION_OVERLAY set, `sequence` is the Xyz monster's zone and `position`
    the material's index)."""

    controller: int
    location: int
    sequence: int
    position: int = 0


@dataclass
class Msg:
    name: str
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def player(self) -> int | None:
        return self.data.get("player")

    def __getitem__(self, key: str) -> Any:
        return self.data[key]


class Reader:
    def __init__(self, buf: bytes):
        self.buf, self.pos = buf, 0

    def _take(self, fmt: str):
        value = struct.unpack_from(fmt, self.buf, self.pos)[0]
        self.pos += struct.calcsize(fmt)
        return value

    def u8(self) -> int:
        return self._take("<B")

    def i8(self) -> int:
        return self._take("<b")

    def u16(self) -> int:
        return self._take("<H")

    def u32(self) -> int:
        return self._take("<I")

    def i32(self) -> int:
        return self._take("<i")

    def u64(self) -> int:
        return self._take("<Q")

    def loc(self) -> Loc:
        return Loc(self.u8(), self.u8(), self.u32(), self.u32())

    def short_loc(self) -> Loc:  # controller, location, sequence as bytes
        return Loc(self.u8(), self.u8(), self.u8())

    def mid_loc(self) -> Loc:  # controller, location as bytes, sequence as u32
        return Loc(self.u8(), self.u8(), self.u32())

    def list(self, count: int, item) -> list:
        return [item() for _ in range(count)]


def _cards(r: Reader, loc) -> list[dict]:
    return r.list(r.u32(), lambda: {"code": r.u32(), "loc": loc()})


def _effects(r: Reader, loc) -> list[dict]:
    return r.list(r.u32(), lambda: {"code": r.u32(), "loc": loc(), "desc": r.u64(), "mode": r.u8()})


def _idlecmd(r: Reader) -> dict:
    d = {"player": r.u8()}
    d["summon"] = _cards(r, r.mid_loc)
    d["spsummon"] = _cards(r, r.mid_loc)
    d["repos"] = _cards(r, r.short_loc)
    d["mset"] = _cards(r, r.mid_loc)
    d["sset"] = _cards(r, r.mid_loc)
    d["activate"] = _effects(r, r.mid_loc)
    d["to_bp"], d["to_ep"], d["shuffle"] = r.u8(), r.u8(), r.u8()
    return d


def _battlecmd(r: Reader) -> dict:
    d = {"player": r.u8()}
    d["activate"] = _effects(r, r.mid_loc)
    d["attack"] = r.list(r.u32(), lambda: {"code": r.u32(), "loc": r.short_loc(), "direct": r.u8()})
    d["to_m2"], d["to_ep"] = r.u8(), r.u8()
    return d


def _select_card(r: Reader) -> dict:
    d = {"player": r.u8(), "cancelable": r.u8(), "min": r.u32(), "max": r.u32()}
    d["cards"] = _cards(r, r.loc)
    return d


def _select_chain(r: Reader) -> dict:
    d = {"player": r.u8(), "spe_count": r.u8(), "forced": r.u8(), "hint": r.u32(), "hint_opponent": r.u32()}
    d["chains"] = r.list(r.u32(), lambda: {"code": r.u32(), "loc": r.loc(), "desc": r.u64(), "mode": r.u8()})
    return d


def _select_tribute(r: Reader) -> dict:
    d = {"player": r.u8(), "cancelable": r.u8(), "min": r.u32(), "max": r.u32()}
    d["cards"] = r.list(r.u32(), lambda: {"code": r.u32(), "loc": r.mid_loc(), "release": r.u8()})
    return d


def _select_counter(r: Reader) -> dict:
    d = {"player": r.u8(), "counter": r.u16(), "count": r.u16()}
    d["cards"] = r.list(r.u32(), lambda: {"code": r.u32(), "loc": r.short_loc(), "counters": r.u16()})
    return d


def _select_sum(r: Reader) -> dict:
    d = {"player": r.u8(), "at_least": r.u8(), "sum": r.u32(), "min": r.u32(), "max": r.u32()}
    d["must"] = r.list(r.u32(), lambda: {"code": r.u32(), "loc": r.loc(), "param": r.u32()})
    d["cards"] = r.list(r.u32(), lambda: {"code": r.u32(), "loc": r.loc(), "param": r.u32()})
    return d


def _unselect_card(r: Reader) -> dict:
    d = {"player": r.u8(), "finishable": r.u8(), "cancelable": r.u8(), "min": r.u32(), "max": r.u32()}
    d["select"] = _cards(r, r.loc)
    d["unselect"] = _cards(r, r.loc)
    return d


def _sort(r: Reader) -> dict:
    d = {"player": r.u8()}
    d["cards"] = r.list(r.u32(), lambda: {"code": r.u32(), "loc": Loc(r.u8(), r.u32(), r.u32())})
    return d


def _options(r: Reader) -> dict:
    return {"player": r.u8(), "options": r.list(r.u8(), r.u64)}


def _confirm(r: Reader) -> dict:
    return {"player": r.u8(), "cards": _cards(r, r.mid_loc)}


def _move(r: Reader) -> dict:
    return {"code": r.u32(), "from": r.loc(), "to": r.loc(), "reason": r.u32()}


def _chain_count(r: Reader) -> dict:
    return {"chain": r.u8()}


def _player_amount(r: Reader) -> dict:
    return {"player": r.u8(), "amount": r.u32()}


def _targets(r: Reader) -> dict:
    return {"cards": r.list(r.u32(), r.loc)}


def _two_cards(r: Reader) -> dict:
    return {"card": r.loc(), "target": r.loc()}


def _counter(r: Reader) -> dict:
    return {"counter": r.u16(), "loc": r.short_loc(), "count": r.u16()}


def _results(r: Reader) -> dict:
    return {"player": r.u8(), "results": r.list(r.u8(), r.u8)}


DECODERS = {
    "HINT": lambda r: {"type": r.u8(), "player": r.u8(), "value": r.u64()},
    "WIN": lambda r: {"player": r.u8(), "reason": r.u8()},
    "SELECT_BATTLECMD": _battlecmd,
    "SELECT_IDLECMD": _idlecmd,
    "SELECT_EFFECTYN": lambda r: {"player": r.u8(), "code": r.u32(), "loc": r.loc(), "desc": r.u64()},
    "SELECT_YESNO": lambda r: {"player": r.u8(), "desc": r.u64()},
    "SELECT_OPTION": _options,
    "SELECT_CARD": _select_card,
    "SELECT_CHAIN": _select_chain,
    "SELECT_PLACE": lambda r: {"player": r.u8(), "count": r.u8(), "blocked": r.u32()},
    "SELECT_DISFIELD": lambda r: {"player": r.u8(), "count": r.u8(), "blocked": r.u32()},
    "SELECT_POSITION": lambda r: {"player": r.u8(), "code": r.u32(), "positions": r.u8()},
    "SELECT_TRIBUTE": _select_tribute,
    "SORT_CHAIN": _sort,
    "SORT_CARD": _sort,
    "SELECT_COUNTER": _select_counter,
    "SELECT_SUM": _select_sum,
    "SELECT_UNSELECT_CARD": _unselect_card,
    "ROCK_PAPER_SCISSORS": lambda r: {"player": r.u8()},
    "ANNOUNCE_RACE": lambda r: {"player": r.u8(), "count": r.u8(), "available": r.u64()},
    "ANNOUNCE_ATTRIB": lambda r: {"player": r.u8(), "count": r.u8(), "available": r.u32()},
    "ANNOUNCE_CARD": _options,
    "ANNOUNCE_NUMBER": _options,
    "CONFIRM_DECKTOP": _confirm,
    "CONFIRM_EXTRATOP": _confirm,
    "CONFIRM_CARDS": _confirm,
    "NEW_TURN": lambda r: {"player": r.u8()},
    "NEW_PHASE": lambda r: {"phase": r.u16()},
    "MOVE": _move,
    "POS_CHANGE": lambda r: {"code": r.u32(), "loc": r.short_loc(), "from": r.u8(), "to": r.u8()},
    "SET": lambda r: {"code": r.u32(), "loc": r.loc()},
    "SWAP": lambda r: {"code": r.u32(), "loc": r.loc(), "code2": r.u32(), "loc2": r.loc()},
    "FIELD_DISABLED": lambda r: {"blocked": r.u32()},
    "SUMMONING": lambda r: {"code": r.u32(), "loc": r.loc()},
    "SPSUMMONING": lambda r: {"code": r.u32(), "loc": r.loc()},
    "FLIPSUMMONING": lambda r: {"code": r.u32(), "loc": r.loc()},
    # `player` activated it; `from_location` is where the card was when it was activated.
    "CHAINING": lambda r: {
        "code": r.u32(),
        "loc": r.loc(),
        "player": r.u8(),
        "from_location": r.u8(),
        "from_sequence": r.u32(),
        "desc": r.u64(),
        "chain": r.u32(),
    },
    "CHAINED": _chain_count,
    "CHAIN_SOLVING": _chain_count,
    "CHAIN_SOLVED": _chain_count,
    "CHAIN_NEGATED": _chain_count,
    "CHAIN_DISABLED": _chain_count,
    "CARD_SELECTED": _targets,
    "BECOME_TARGET": _targets,
    "RANDOM_SELECTED": lambda r: {"player": r.u8(), "cards": r.list(r.u32(), r.loc)},
    "DRAW": lambda r: {"player": r.u8(), "cards": r.list(r.u32(), lambda: {"code": r.u32(), "pos": r.u32()})},
    "DAMAGE": _player_amount,
    "RECOVER": _player_amount,
    "LPUPDATE": _player_amount,
    "PAY_LPCOST": _player_amount,
    "EQUIP": _two_cards,
    "CARD_TARGET": _two_cards,
    "CANCEL_TARGET": _two_cards,
    "ADD_COUNTER": _counter,
    "REMOVE_COUNTER": _counter,
    "ATTACK": lambda r: {"attacker": r.loc(), "target": r.loc()},
    "BATTLE": lambda r: {
        "attacker": r.loc(),
        "atk": r.u32(),
        "def": r.u32(),
        "attacker_destroyed": r.u8(),
        "target": r.loc(),
        "target_atk": r.u32(),
        "target_def": r.u32(),
        "target_destroyed": r.u8(),
    },
    "MISSED_EFFECT": lambda r: {"loc": r.loc(), "code": r.u32()},
    "TOSS_COIN": _results,
    "TOSS_DICE": _results,
    "HAND_RES": lambda r: {"hands": r.u8()},
    "CARD_HINT": lambda r: {"loc": r.loc(), "type": r.u8(), "value": r.u64()},
    "PLAYER_HINT": lambda r: {"player": r.u8(), "type": r.u8(), "value": r.u64()},
    "SHOW_HINT": lambda r: {"text": r.buf[r.pos + 2 :].split(b"\0")[0].decode(errors="replace")},
    "MATCH_KILL": lambda r: {"code": r.u32()},
}


def decode(buffer: bytes) -> list[Msg]:
    """Splits an OCG_DuelGetMessage buffer (u32 length + message, repeated) into messages."""
    out, pos = [], 0
    while pos + 4 <= len(buffer):
        (size,) = struct.unpack_from("<I", buffer, pos)
        body = buffer[pos + 4 : pos + 4 + size]
        pos += 4 + size
        if not body:
            continue
        name = NAMES.get(body[0], f"MSG_{body[0]}")
        decoder = DECODERS.get(name)
        msg = Msg(name)
        if decoder:
            try:
                msg.data = decoder(Reader(body[1:]))
            except struct.error as e:  # a layout change in the engine would land here
                msg.data = {"decode_error": str(e), "raw": body[1:].hex()}
        out.append(msg)
    return out
