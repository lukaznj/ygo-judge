"""Board setups: the structured scenario the agent writes, turned into an EDOPro puzzle script.

The same Lua runs in the headless engine and opens in EDOPro's Puzzle mode, where one person
controls both players (no DUEL_SIMPLE_AI) and the duel goes on past the first turn (no
aux.BeginPuzzle).
"""

import re
from typing import Literal

from pydantic import BaseModel, Field

from .cards import EXTRA_DECK_TYPES, TYPE_FUSION, TYPE_LINK, TYPE_MONSTER, TYPE_RITUAL, TYPE_SYNCHRO, TYPE_XYZ, Card, CardDB, normalize

CardRef = str | int

SUMMON_TYPES = {
    "normal": "SUMMON_TYPE_NORMAL",
    "tribute": "SUMMON_TYPE_TRIBUTE",
    "flip": "SUMMON_TYPE_FLIP",
    "special": "SUMMON_TYPE_SPECIAL",
    "fusion": "SUMMON_TYPE_FUSION",
    "ritual": "SUMMON_TYPE_RITUAL",
    "synchro": "SUMMON_TYPE_SYNCHRO",
    "xyz": "SUMMON_TYPE_XYZ",
    "pendulum": "SUMMON_TYPE_PENDULUM",
    "link": "SUMMON_TYPE_LINK",
}
SUMMON_FROM = {
    "hand": "LOCATION_HAND",
    "deck": "LOCATION_DECK",
    "extra deck": "LOCATION_EXTRA",
    "graveyard": "LOCATION_GRAVE",
    "banished": "LOCATION_REMOVED",
}
POSITIONS = {"attack": "POS_FACEUP_ATTACK", "defense": "POS_FACEUP_DEFENSE", "set": "POS_FACEDOWN_DEFENSE"}


class Monster(BaseModel):
    card: CardRef = Field(description="Card name or passcode.")
    zone: int | Literal["EMZ left", "EMZ right"] | None = Field(
        None,
        description="Main Monster Zone 1-5 (left to right as its controller sees it) or an Extra Monster Zone. Default: next free zone.",
    )
    position: Literal["attack", "defense", "set"] = Field("attack", description='"set" = face-down Defense Position.')
    summoned: Literal[tuple(SUMMON_TYPES)] | None = Field(
        None, description="How it was summoned. Default: its Extra Deck summon type for Extra Deck monsters, otherwise Normal Summon."
    )
    summoned_from: Literal[tuple(SUMMON_FROM)] | None = Field(
        None, description='Where it was summoned from. Default: "extra deck" for Extra Deck monsters, otherwise "hand".'
    )
    materials: list[CardRef] = Field(default_factory=list, description="Xyz Materials attached to it.")
    counters: dict[str, int] = Field(default_factory=dict, description='Counters on it, e.g. {"Spell Counter": 2}.')


class SpellTrap(BaseModel):
    card: CardRef = Field(description="Card name or passcode.")
    zone: int | None = Field(None, description="Spell & Trap Zone 1-5. Default: next free zone.")
    face_up: bool = Field(False, description="false = Set (face-down). Set cards can be activated right away.")
    equipped_to: str | None = Field(
        None, description='The monster it is equipped to: a card name on the field, prefixed with "p1:" or "p2:" if needed.'
    )
    targets: list[str] = Field(
        default_factory=list, description="Cards it continuously targets (e.g. Call of the Haunted's monster), same format as equipped_to."
    )
    counters: dict[str, int] = Field(default_factory=dict)


class FieldSpell(BaseModel):
    card: CardRef
    face_up: bool = True
    counters: dict[str, int] = Field(default_factory=dict)


class PlayerSetup(BaseModel):
    lp: int = 8000
    hand: list[CardRef] = Field(default_factory=list)
    deck: list[CardRef] = Field(default_factory=list, description="Main Deck, top card first. Empty unless listed.")
    extra_deck: list[CardRef] = Field(default_factory=list)
    graveyard: list[CardRef] = Field(default_factory=list)
    banished: list[CardRef] = Field(default_factory=list, description="Face-up banished cards.")
    monsters: list[Monster] = Field(default_factory=list)
    spells_traps: list[SpellTrap] = Field(default_factory=list)
    field_spell: FieldSpell | None = None
    pendulum_zones: list[CardRef] = Field(default_factory=list, description="Up to 2 Pendulum Scales: [left, right].")
    deck_filler: int = Field(
        0, description="Extra vanilla cards (Mystical Shine Ball) under the listed deck, for effects that draw, mill or need a deck."
    )


class Setup(BaseModel):
    """A board to test on. P1 is the turn player: the duel starts in P1's Main Phase 1 of turn 1,
    and P1 may conduct a Battle Phase. Both players are controlled by you."""

    p1: PlayerSetup = Field(default_factory=PlayerSetup)
    p2: PlayerSetup = Field(default_factory=PlayerSetup)
    title: str = Field("", description="Short description of what is being tested.")
    seed: int = Field(1, description="Random seed (coin tosses, dice, random selections).")


FILLER = 39552864  # Mystical Shine Ball: vanilla, no archetype


def _lua_string(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


class _Builder:
    def __init__(self, cards: CardDB):
        self.cards = cards
        self.lines: list[str] = []
        self.placed: list[tuple[int, str, Card]] = []  # (player, variable, card) on the field
        self.count = 0

    def card(self, ref: CardRef) -> Card:
        return self.cards.resolve(ref)

    def add(self, card: Card, player: int, location: str, seq: int, position: str, proc: bool = False) -> str:
        self.count += 1
        var = f"c{self.count}"
        extra = ",true" if proc else ""
        self.lines.append(f"local {var}=Debug.AddCard({card.code},{player},{player},{location},{seq},{position}{extra}) --{card.name}")
        return var

    def counters(self, var: str, counters: dict[str, int]) -> None:
        for name, count in counters.items():
            counter = self.cards.counter_by_name.get(normalize(name))
            if counter is None:
                raise ValueError(f'Unknown counter "{name}".')
            self.lines.append(f"Debug.PreAddCounter({var},{counter:#x},{count})")

    def find(self, ref: str, player: int) -> str:
        """Variable of a card on the field from "name", "p1:name" or "p2:name"."""
        side = None
        if m := re.match(r"\s*p([12])\s*:\s*(.+)", ref, re.IGNORECASE):
            side, ref = int(m[1]) - 1, m[2]
        key = normalize(ref)
        matches = [v for p, v, c in self.placed if normalize(c.name) == key and side in (None, p)]
        if len(matches) != 1:
            where = "on the field" if not matches else "more than once; prefix it with p1: or p2:"
            raise ValueError(f'"{ref}" is {"not " if not matches else ""}{where}.')
        return matches[0]


def build_puzzle(setup: Setup, cards: CardDB) -> str:
    """EDOPro puzzle script for a setup; raises ValueError on an invalid setup."""
    b = _Builder(cards)
    b.lines += [
        f"--YGO Judge scenario{': ' + setup.title.splitlines()[0] if setup.title else ''}",
        "--Both players are yours: play P2's responses too.",
        'Debug.SetAIName("P2")',
        "Debug.ReloadFieldBegin(DUEL_ATTACK_FIRST_TURN+DUEL_PSEUDO_SHUFFLE,5)",
        f"Debug.SetPlayerInfo(0,{setup.p1.lp},0,0)",
        f"Debug.SetPlayerInfo(1,{setup.p2.lp},0,0)",
    ]
    links: list[tuple[int, str, SpellTrap]] = []
    for player, ps in enumerate((setup.p1, setup.p2)):
        tag = f"P{player + 1}"
        b.lines.append(f"--{tag}")
        for _ in range(ps.deck_filler):
            b.add(cards.get(FILLER), player, "LOCATION_DECK", 0, "POS_FACEDOWN")
        for ref in reversed(ps.deck):  # each card added goes on top, so the first listed ends up on top
            card = b.card(ref)
            if card.is_(TYPE_MONSTER) and card.is_(EXTRA_DECK_TYPES):
                raise ValueError(f"{card.name} is an Extra Deck monster; list it in extra_deck.")
            b.add(card, player, "LOCATION_DECK", 0, "POS_FACEDOWN")
        for ref in ps.extra_deck:
            card = b.card(ref)
            if not (card.is_(TYPE_MONSTER) and card.is_(EXTRA_DECK_TYPES)):
                raise ValueError(f"{card.name} is not an Extra Deck monster.")
            b.add(card, player, "LOCATION_EXTRA", 0, "POS_FACEDOWN")
        for ref in ps.hand:
            card = b.card(ref)
            if card.is_(TYPE_MONSTER) and card.is_(EXTRA_DECK_TYPES):
                raise ValueError(f"{card.name} is an Extra Deck monster and can't be in the hand.")
            b.add(card, player, "LOCATION_HAND", 0, "POS_FACEDOWN")
        for ref in ps.graveyard:
            b.add(b.card(ref), player, "LOCATION_GRAVE", 0, "POS_FACEUP")
        for ref in ps.banished:
            b.add(b.card(ref), player, "LOCATION_REMOVED", 0, "POS_FACEUP")

        used = set()
        for m in ps.monsters:
            card = b.card(m.card)
            if not card.is_(TYPE_MONSTER):
                raise ValueError(f"{card.name} is not a monster; put it in spells_traps.")
            if m.zone in ("EMZ left", "EMZ right"):
                seq = 5 if m.zone == "EMZ left" else 6
            elif m.zone is None:
                seq = next((s for s in range(5) if s not in used), None)
                if seq is None:
                    raise ValueError(f"{tag} has no free Main Monster Zone for {card.name}.")
            elif 1 <= m.zone <= 5:
                seq = m.zone - 1
            else:
                raise ValueError(f"Monster zone {m.zone} doesn't exist; use 1-5, 'EMZ left' or 'EMZ right'.")
            if seq in used:
                raise ValueError(f"{tag} Monster Zone {m.zone} is used twice.")
            used.add(seq)
            var = b.add(card, player, "LOCATION_MZONE", seq, POSITIONS[m.position], proc=True)
            b.placed.append((player, var, card))
            summoned = m.summoned or _natural_summon(card)
            source = m.summoned_from or ("extra deck" if card.is_(EXTRA_DECK_TYPES) else "hand")
            b.lines.append(f"Debug.PreSummon({var},{SUMMON_TYPES[summoned]},{SUMMON_FROM[source]})")
            for ref in m.materials:
                material = b.card(ref)
                b.lines.append(
                    f"Debug.AddCard({material.code},{player},{player},LOCATION_MZONE,{seq},POS_FACEUP) --{material.name} (material)"
                )
            b.counters(var, m.counters)

        pendulum = [b.card(ref) for ref in ps.pendulum_zones]
        if len(pendulum) > 2:
            raise ValueError(f"{tag} has only 2 Pendulum Zones.")
        used = {0, 4} if len(pendulum) == 2 else ({0} if pendulum else set())  # Pendulum Zones are S/T zones 1 and 5
        for index, card in enumerate(pendulum):
            b.placed.append((player, b.add(card, player, "LOCATION_PZONE", index, "POS_FACEUP"), card))
        for st in ps.spells_traps:
            card = b.card(st.card)
            if st.zone is None:
                seq = next((s for s in range(5) if s not in used), None)
                if seq is None:
                    raise ValueError(f"{tag} has no free Spell & Trap Zone for {card.name}.")
            elif 1 <= st.zone <= 5:
                seq = st.zone - 1
            else:
                raise ValueError(f"Spell & Trap Zone {st.zone} doesn't exist; use 1-5.")
            if seq in used:
                raise ValueError(f"{tag} Spell & Trap Zone {seq + 1} is already used.")
            used.add(seq)
            var = b.add(card, player, "LOCATION_SZONE", seq, "POS_FACEUP" if st.face_up else "POS_FACEDOWN")
            b.placed.append((player, var, card))
            links.append((player, var, st))
        if ps.field_spell:
            card = b.card(ps.field_spell.card)
            var = b.add(card, player, "LOCATION_FZONE", 0, "POS_FACEUP" if ps.field_spell.face_up else "POS_FACEDOWN")
            b.placed.append((player, var, card))
            b.counters(var, ps.field_spell.counters)

    for player, var, st in links:  # equips and targets can point at either player's cards
        if st.equipped_to:
            b.lines.append(f"Debug.PreEquip({var},{b.find(st.equipped_to, player)})")
        for ref in st.targets:
            b.lines.append(f"Debug.PreSetTarget({var},{b.find(ref, player)})")
        b.counters(var, st.counters)
    b.lines.append("Debug.ReloadFieldEnd()")
    if setup.title:
        b.lines.append(f"Debug.ShowHint({_lua_string(setup.title)})")
    return "\n".join(b.lines) + "\n"


def _natural_summon(card: Card) -> str:
    for bit, kind in ((TYPE_LINK, "link"), (TYPE_XYZ, "xyz"), (TYPE_SYNCHRO, "synchro"), (TYPE_FUSION, "fusion"), (TYPE_RITUAL, "ritual")):
        if card.is_(bit):
            return kind
    return "normal"
