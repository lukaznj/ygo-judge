"""A test duel: the engine driven one decision at a time, with a readable log and board.

Every response sent to the engine is recorded, so a duel can be rebuilt (after a restart, or to
undo decisions) by replaying the same setup and responses: the engine is deterministic.
"""

import struct
from collections import deque
from dataclasses import dataclass

from . import engine as E
from .cards import CardDB, normalize
from .messages import PROMPTS, Loc, Msg, decode
from .scenario import Setup, build_puzzle

PHASES = {
    0x01: "Draw Phase",
    0x02: "Standby Phase",
    0x04: "Main Phase 1",
    0x08: "Battle Phase",
    0x10: "Battle Step",
    0x20: "Damage Step",
    0x40: "Damage Calculation",
    0x80: "End Step of the Battle Phase",
    0x100: "Main Phase 2",
    0x200: "End Phase",
}
POSITIONS = {1: "face-up Attack", 2: "face-down Attack", 4: "face-up Defense", 8: "face-down Defense"}
MAX_STEPS = 300  # responses per call; a runaway loop stops here

HINT_EVENT, HINT_MESSAGE, HINT_SELECTMSG, HINT_OPSELECTED = 1, 2, 3, 4
HINT_RACE, HINT_ATTRIB, HINT_CODE, HINT_NUMBER, HINT_CARD = 6, 7, 8, 9, 10

REASON_DESTROY, REASON_RELEASE, REASON_MATERIAL = 0x1, 0x2, 0x8
REASON_SUMMON, REASON_BATTLE, REASON_EFFECT, REASON_COST = 0x10, 0x20, 0x40, 0x80
REASON_RULE, REASON_SPSUMMON, REASON_DISCARD, REASON_DRAW = 0x400, 0x800, 0x4000, 0x2000000
MATERIAL_KINDS = {0x40000: "Fusion", 0x80000: "Synchro", 0x100000: "Ritual", 0x200000: "Xyz", 0x10000000: "Link"}
STATUS_DISABLED, STATUS_SET_TURN = 0x1, 0x10

BOARD_QUERY = (
    E.QUERY_CODE
    | E.QUERY_POSITION
    | E.QUERY_TYPE
    | E.QUERY_LEVEL
    | E.QUERY_RANK
    | E.QUERY_ATTACK
    | E.QUERY_DEFENSE
    | E.QUERY_BASE_ATTACK
    | E.QUERY_BASE_DEFENSE
    | E.QUERY_OVERLAY_CARD
    | E.QUERY_COUNTERS
    | E.QUERY_OWNER
    | E.QUERY_STATUS
    | E.QUERY_LINK
    | E.QUERY_EQUIP_CARD
)


class ChoiceError(ValueError):
    pass


@dataclass
class Option:
    label: str
    response: bytes | None = None  # answer when this option alone is picked
    value: object = None  # data for multi-pick answers (index, zone, bit...)


@dataclass
class Decision:
    kind: str
    player: int
    title: str
    options: list[Option]
    pick: tuple[int, int] | None = None  # (min, max) options for multi-pick decisions
    auto: bytes | None = None  # answer when nothing was chosen; None = the agent must decide
    cancel: bytes | None = None
    hint: str = ""


def _i32(value: int) -> bytes:
    return struct.pack("<i", value)


def _idle(kind: int, index: int = 0) -> bytes:
    return _i32((index << 16) | kind)


def _tokens(text: str) -> list[str]:
    return normalize(text).split()


def _contains(haystack: list[str], needle: list[str]) -> bool:
    n = len(needle)
    return any(haystack[i : i + n] == needle for i in range(len(haystack) - n + 1))


class Duel:
    def __init__(self, cards: CardDB, setup: Setup, duel_id: str = "", responses: list[tuple[bytes, bool]] = ()):
        self.cards = cards
        self.setup = setup
        self.id = duel_id
        self.puzzle = build_puzzle(setup, cards)
        self.log: list[str] = []
        self.errors: list[str] = []
        self.responses: list[tuple[bytes, bool]] = []  # (response, chosen by the agent)
        self.turn, self.turn_player, self.phase = 0, 0, 0
        self.lp = [setup.p1.lp, setup.p2.lp]
        self.chain: list[dict] = []
        self.select_hint: dict[int, int] = {}
        self.event_hint = ""
        self.decision: Decision | None = None
        self.result = ""
        self.decisions = 0
        self.stop_at_every_window = False
        self._retry = False
        self._tributed = False  # a Tribute for a summon was just made
        self._finish_unselect = False
        self.engine = E.Engine(setup.seed, E.DUEL_MODE_MR5, cards.fill, cards.script, self._on_log)
        for lib in ("constant.lua", "utility.lua"):
            if not self.engine.load_script(cards.script(lib) or b"", lib):
                raise RuntimeError(f"Could not load {lib}: {self.errors[-1:] or 'missing'}")
        if not self.engine.load_script(self.puzzle.encode(), "scenario.lua"):
            raise ValueError("The board setup failed in the engine: " + " | ".join(self.errors[-3:]))
        decode(self.engine.messages())  # setup messages (field reload, hint); nothing to log
        self.engine.start()
        self._run()
        for response, chosen in responses:
            self._send(response, chosen)

    # ------------------------------------------------------------------ engine loop
    def _on_log(self, text: str, kind: int) -> None:
        if kind == E.LOG_ERROR:
            self.errors.append(text.strip())

    def _run(self) -> None:
        """Process until the engine waits for an answer or the duel ends."""
        for _ in range(100_000):
            status = self.engine.process()
            prompt = None
            for msg in decode(self.engine.messages()):
                if msg.name == "RETRY":
                    self._retry = True
                elif msg.name in PROMPTS:
                    prompt = msg
                else:
                    self._record(msg)
            if status == E.STATUS_END or self.result:
                self.decision = None
                return
            if status == E.STATUS_AWAITING:
                if prompt is not None:
                    self.decision = self._decision(prompt)
                elif not self._retry:
                    raise RuntimeError("The engine is waiting without asking anything.")
                return
        raise RuntimeError("The engine did not stop (possible infinite loop in a card script).")

    def _send(self, response: bytes, chosen: bool) -> None:
        self._retry = False
        self.responses.append((response, chosen))
        if chosen:
            self.decisions += 1
        self.engine.respond(response)
        self._run()
        if self._retry:
            self.responses.pop()
            if chosen:
                self.decisions -= 1
            raise ChoiceError("The engine rejected that answer (not a legal selection).")

    # ------------------------------------------------------------------ playing
    def play(self, choices: list) -> str:
        """Answer decisions with `choices` (each used at the first decision it fits), handle
        trivial decisions automatically, and stop at the next real decision."""
        start, errors = len(self.log), len(self.errors)
        queue, notes = deque(choices), []
        for _ in range(MAX_STEPS):
            d = self.decision
            if d is None:
                break
            finish = self._finish_unselect
            self._finish_unselect = False
            if finish and d.kind == "unselect" and d.cancel is not None and any(o.label.startswith("Unselect ") for o in d.options):
                self._send(d.cancel, False)  # the listed cards are all picked: finish that selection
                continue
            response = None
            if queue:
                try:
                    response = self._answer(d, queue[0], auto=d.auto is not None)
                except ChoiceError as e:
                    if d.auto is None:
                        notes.append(f"Choice {queue[0]!r} not used: {e}")
                        break
            if response is not None:
                choice = queue.popleft()
                if isinstance(response, tuple):  # one pick of an unselect selection
                    response, rest, from_list = response
                    if rest:
                        queue.appendleft(rest)
                    elif from_list:
                        self._finish_unselect = True
                try:
                    self._send(response, True)
                except ChoiceError as e:
                    notes.append(f"Choice {choice!r}: {e}")
                    break
            elif d.auto is not None:
                self._send(d.auto, False)
            else:
                if queue:
                    notes.append(f"Stopped: {queue[0]!r} doesn't fit this decision.")
                break
        else:
            notes.append(f"Stopped after {MAX_STEPS} steps.")
        if queue and not any(n.startswith(("Stopped", "Choice")) for n in notes):
            notes.append(f"Unused choices: {list(queue)}")
        return self.render(start, errors, notes)

    def _answer(self, d: Decision, choice, auto: bool) -> bytes | tuple | None:
        """The engine answer for `choice` at decision `d`, or None when it doesn't fit."""
        if isinstance(choice, int) and not isinstance(choice, bool) and auto:
            return None  # numbers refer to options the agent saw, never to skipped decisions
        if d.kind == "announce_card":
            if not isinstance(choice, str):
                return None
            try:
                return _i32(self.cards.resolve(choice).code)
            except ValueError as e:
                raise ChoiceError(str(e)) from None
        if isinstance(choice, str) and normalize(choice) == "cancel" and d.cancel is not None and d.kind != "unselect":
            return d.cancel
        if d.kind == "unselect":
            items = choice if isinstance(choice, list) else [choice]
            if not items:
                return None
            index = self._index(d, items[0])
            if index is None:
                return None
            return d.options[index].response, items[1:], isinstance(choice, list)
        if d.pick is None:
            if isinstance(choice, list):
                if len(choice) != 1:
                    raise ChoiceError("this decision takes one option, not a list")
                choice = choice[0]
            index = self._index(d, choice)
            if index is None or (auto and d.options[index].response == d.auto and d.kind == "chain"):
                return None  # at a skipped chain window, only an actual activation counts
            return d.options[index].response
        items = choice if isinstance(choice, list) else [choice]
        taken: list[int] = []
        for item in items:
            index = self._index(d, item, taken if d.kind != "counter" else ())
            if index is None:
                return None
            taken.append(index)
        low, high = d.pick
        if not low <= len(taken) <= high:
            raise ChoiceError(f"pick {low}{'-' + str(high) if high != low else ''} option(s), got {len(taken)}")
        return self._multi(d, taken)

    def _index(self, d: Decision, choice, taken=()) -> int | None:
        if isinstance(choice, bool):
            choice = "yes" if choice else "no"
        if isinstance(choice, str) and choice.strip().isdigit():
            choice = int(choice)
        if isinstance(choice, int):
            if 0 <= choice < len(d.options) and choice not in taken:
                return choice
            raise ChoiceError(f"there is no option {choice} here (0-{len(d.options) - 1})")
        if not isinstance(choice, str):
            raise ChoiceError(f"can't use {choice!r} as a choice")
        key = _tokens(choice)
        if not key:
            return None
        labels = [_tokens(o.label) for o in d.options]
        tests = (
            lambda words: words == key,
            lambda words: words[: len(key)] == key,
            lambda words: _contains(words, key),
            lambda words: all(k in words for k in key),
        )
        for test in tests:
            hits = [i for i, words in enumerate(labels) if test(words) and i not in taken]
            if not hits:
                continue
            if len({d.options[i].label for i in hits}) == 1:  # identical options, e.g. two copies
                return hits[0]
            listing = "; ".join(f"[{i}] {d.options[i].label}" for i in hits)
            raise ChoiceError(f'"{choice}" matches several options ({listing}); use the number')
        return None

    def _multi(self, d: Decision, picks: list[int]) -> bytes:
        values = [d.options[i].value for i in picks]
        if d.kind in ("cards", "tribute", "sum"):
            return struct.pack("<iI", 0, len(values)) + b"".join(struct.pack("<I", v) for v in values)
        if d.kind in ("place", "disfield"):
            return b"".join(bytes(v) for v in values)
        if d.kind == "counter":
            counts = [0] * len(d.options)
            for i in picks:
                counts[i] += 1
            return b"".join(struct.pack("<h", c) for c in counts)
        if d.kind in ("sort_chain", "sort_card"):
            order = [0] * len(d.options)
            for position, i in enumerate(picks):
                order[i] = position
            return bytes(order)
        if d.kind == "race":
            return struct.pack("<Q", sum(values))
        if d.kind == "attribute":
            return struct.pack("<I", sum(values))
        raise ChoiceError(f"can't combine options for a {d.kind} decision")

    def undo(self, decisions: int) -> str:
        """Rebuild the duel without the last `decisions` choices."""
        chosen = [i for i, (_, by_agent) in enumerate(self.responses) if by_agent]
        if decisions < 1 or decisions > len(chosen):
            raise ChoiceError(f"can undo 1-{len(chosen)} decision(s)")
        keep = self.responses[: chosen[-decisions]]
        rebuilt = Duel(self.cards, self.setup, self.id, keep)
        self.__dict__.update(rebuilt.__dict__)
        return self.render(len(self.log), len(self.errors), [f"Undid {decisions} decision(s)."], full_log=True)

    # ------------------------------------------------------------------ text helpers
    def name(self, code: int) -> str:
        return self.cards.name(code) if code else "a face-down card"

    def where(self, loc: Loc) -> str:
        p = f"P{loc.controller + 1}"
        location, seq = loc.location, loc.sequence
        if location & E.LOCATION_OVERLAY:
            return f"{p} Xyz Material"
        if location == E.LOCATION_MZONE:
            return f"{p} Monster Zone {seq + 1}" if seq < 5 else f"{p} Extra Monster Zone {'left' if seq == 5 else 'right'}"
        if location == E.LOCATION_SZONE:
            if seq < 5:
                return f"{p} Spell & Trap Zone {seq + 1}"
            return f"{p} Field Zone" if seq == 5 else f"{p} Pendulum Zone {'left' if seq == 6 else 'right'}"
        names = {
            E.LOCATION_HAND: "hand",
            E.LOCATION_DECK: "Deck",
            E.LOCATION_GRAVE: "GY",
            E.LOCATION_REMOVED: "banished",
            E.LOCATION_EXTRA: "Extra Deck",
        }
        return f"{p} {names.get(location, f'location {location:#x}')}"

    def label(self, code: int, loc: Loc) -> str:
        return f"{self.name(code)} ({self.where(loc)})"

    def code_at(self, loc: Loc) -> int:
        if not loc.location or loc.location & E.LOCATION_OVERLAY:
            return 0
        info = parse_card(self.engine.query(loc.controller, loc.location, loc.sequence, E.QUERY_CODE))
        return info.get("code", 0) if info else 0

    def at(self, loc: Loc) -> str:
        return self.label(self.code_at(loc), loc)

    def effect_text(self, desc: int, code: int = 0) -> str:
        text = self.cards.desc(desc) if desc else ""
        if not text or text.startswith("#"):
            return ""
        return text.replace("[%ls]", self.name(code)).replace("%ls", self.name(code))

    def effect(self, desc: int, code: int) -> str:
        text = self.effect_text(desc, code)
        return f': "{text}"' if text and normalize(text) != "activate" else ""

    # ------------------------------------------------------------------ decisions
    def _decision(self, m: Msg) -> Decision:
        d, p = m.data, m.player
        hint = self.effect_text(self.select_hint.pop(p, 0))
        who = f"P{p + 1}"
        ask = getattr(self, "_ask_" + m.name.lower())
        decision = ask(d, p, who, hint)
        decision.hint = hint
        return decision

    def _ask_select_idlecmd(self, d, p, who, hint) -> Decision:
        opts = [
            Option(f"Activate {self.label(c['code'], c['loc'])}{self.effect(c['desc'], c['code'])}", _idle(5, i))
            for i, c in enumerate(d["activate"])
        ]
        for kind, key, verb in (
            (0, "summon", "Normal Summon"),
            (1, "spsummon", "Special Summon"),
            (3, "mset", "Set monster"),
            (4, "sset", "Set"),
            (2, "repos", "Change the battle position of"),
        ):
            opts += [Option(f"{verb} {self.label(c['code'], c['loc'])}", _idle(kind, i)) for i, c in enumerate(d[key])]
        if d["to_bp"]:
            opts.append(Option("Enter the Battle Phase", _idle(6)))
        if d["to_ep"]:
            opts.append(Option("End the turn", _idle(7)))
        return Decision("idle", p, f"{who}, {PHASES.get(self.phase, 'Main Phase')}: choose an action", opts)

    def _ask_select_battlecmd(self, d, p, who, hint) -> Decision:
        opts = [
            Option(f"Activate {self.label(c['code'], c['loc'])}{self.effect(c['desc'], c['code'])}", _idle(0, i))
            for i, c in enumerate(d["activate"])
        ]
        opts += [
            Option(f"Attack with {self.label(c['code'], c['loc'])}{' (can attack directly)' if c['direct'] else ''}", _idle(1, i))
            for i, c in enumerate(d["attack"])
        ]
        if d["to_m2"]:
            opts.append(Option("Go to Main Phase 2", _idle(2)))
        if d["to_ep"]:
            opts.append(Option("End the turn", _idle(3)))
        return Decision("battle", p, f"{who}, Battle Phase: choose an action", opts)

    def _ask_select_chain(self, d, p, who, hint) -> Decision:
        opts = [
            Option(f"Activate {self.label(c['code'], c['loc'])}{self.effect(c['desc'], c['code'])}", _i32(i))
            for i, c in enumerate(d["chains"])
        ]
        if d["forced"]:
            title = f"{who} must activate a trigger effect: pick the next one to put on the chain"
            auto = _i32(0) if len(opts) == 1 else None
        else:
            opts.append(Option("Pass (don't respond)", _i32(-1)))
            if self.chain:
                link = self.chain[-1]
                title = f"{who} may respond to Chain Link {len(self.chain)} ({self.name(link['code'])})"
            else:
                title = f"{who} may activate an effect{f' ({self.event_hint})' if self.event_hint else ''}"
            idle = not d["chains"] or (d["spe_count"] == 0 and not self.stop_at_every_window)
            auto = _i32(-1) if idle else None
        return Decision("chain", p, title, opts, auto=auto)

    def _ask_select_effectyn(self, d, p, who, hint) -> Decision:
        text = self.effect_text(d["desc"], d["code"])
        title = f"{who}: activate/use the effect of {self.label(d['code'], d['loc'])}?"
        if text and "activate" not in normalize(text):
            title += f" ({text})"
        return Decision("effect_yn", p, title, [Option("Yes", _i32(1)), Option("No", _i32(0))])

    def _ask_select_yesno(self, d, p, who, hint) -> Decision:
        return Decision(
            "yesno", p, f"{who}: {self.effect_text(d['desc']) or 'yes or no?'}", [Option("Yes", _i32(1)), Option("No", _i32(0))]
        )

    def _ask_select_option(self, d, p, who, hint) -> Decision:
        opts = [Option(self.effect_text(o) or f"option {i + 1}", _i32(i)) for i, o in enumerate(d["options"])]
        return Decision("option", p, f"{who}: {hint or 'choose an option'}", opts, auto=_i32(0) if len(opts) == 1 else None)

    def _ask_select_card(self, d, p, who, hint) -> Decision:
        opts = [Option(self.label(c["code"], c["loc"]), value=i) for i, c in enumerate(d["cards"])]
        low, high, cancel = d["min"], d["max"], d["cancelable"]
        decision = Decision("cards", p, f"{who}: {hint or 'select card(s)'}", opts, pick=(low, high), cancel=_i32(-1) if cancel else None)
        if not cancel and low == high == len(opts):  # everything must be picked anyway
            decision.auto = self._multi(decision, list(range(len(opts))))
        return decision

    def _ask_select_tribute(self, d, p, who, hint) -> Decision:
        opts = [
            Option(self.label(c["code"], c["loc"]) + (f" (counts as {c['release']} Tributes)" if c["release"] > 1 else ""), value=i)
            for i, c in enumerate(d["cards"])
        ]
        need = f"{d['min']}" + (f"-{d['max']}" if d["max"] != d["min"] else "")
        return Decision(
            "tribute",
            p,
            f"{who}: select monsters to Tribute ({need} Tribute{'s' if d['max'] > 1 else ''})",
            opts,
            pick=(1, len(opts)),
            cancel=_i32(-1) if d["cancelable"] else None,
        )

    def _ask_select_sum(self, d, p, who, hint) -> Decision:
        opts = [Option(f"{self.label(c['code'], c['loc'])} [{self._sum_param(c['param'])}]", value=i) for i, c in enumerate(d["cards"])]
        rule = "at least" if d["at_least"] else "exactly"
        must = "; already included: " + ", ".join(self.label(c["code"], c["loc"]) for c in d["must"]) if d["must"] else ""
        title = f"{who}: {hint or 'select cards'} — values must add up to {rule} {d['sum']}{must}"
        return Decision("sum", p, title, opts, pick=(1, len(opts)))

    @staticmethod
    def _sum_param(param: int) -> str:
        low, high = param & 0xFFFF, param >> 16
        return f"{low} or {high}" if high and high != low else str(low)

    def _ask_select_unselect_card(self, d, p, who, hint) -> Decision:
        opts = [Option("Select " + self.label(c["code"], c["loc"]), struct.pack("<ii", 1, i)) for i, c in enumerate(d["select"])]
        base = len(d["select"])
        opts += [
            Option("Unselect " + self.label(c["code"], c["loc"]), struct.pack("<ii", 1, base + i)) for i, c in enumerate(d["unselect"])
        ]
        end = None
        if d["finishable"]:
            opts.append(Option("Finish", _i32(-1)))
            end = _i32(-1)
        elif d["cancelable"]:
            opts.append(Option("Cancel", _i32(-1)))
        title = f"{who}: {hint or 'select cards'} (one at a time; {d['min']}-{d['max']} in total)"
        return Decision("unselect", p, title, opts, cancel=end)

    def _ask_select_place(self, d, p, who, hint, kind="place") -> Decision:
        zones = []
        for side in (p, 1 - p):
            base = 0 if side == p else 16
            for location, first_bit, count in ((E.LOCATION_MZONE, 0, 7), (E.LOCATION_SZONE, 8, 8)):
                for seq in range(count):
                    if not d["blocked"] >> (base + first_bit + seq) & 1:
                        zones.append((side, location, seq))
        opts = [Option(self.where(Loc(*z)), value=z) for z in zones]
        count = d["count"]
        decision = Decision(kind, p, f"{who}: {hint or 'choose a zone'}", opts, pick=(count, count))
        if kind == "place":
            decision.auto = self._multi(decision, list(range(count)))
        return decision

    def _ask_select_disfield(self, d, p, who, hint) -> Decision:
        return self._ask_select_place(d, p, who, hint or "choose zone(s) to make unusable", "disfield")

    def _ask_select_position(self, d, p, who, hint) -> Decision:
        opts = [Option(POSITIONS[bit].capitalize(), _i32(bit)) for bit in (1, 2, 4, 8) if d["positions"] & bit]
        default = next(o.response for o in opts)
        return Decision("position", p, f"{who}: choose the battle position of {self.name(d['code'])}", opts, auto=default)

    def _ask_select_counter(self, d, p, who, hint) -> Decision:
        counter = self.cards.counters.get(d["counter"], "counter")
        opts = [Option(f"{self.name(c['code'])} ({self.where(c['loc'])}, {c['counters']})", value=i) for i, c in enumerate(d["cards"])]
        return Decision("counter", p, f"{who}: remove {d['count']} {counter}(s)", opts, pick=(d["count"], d["count"]))

    def _ask_sort_chain(self, d, p, who, hint, kind="sort_chain") -> Decision:
        opts = [Option(self.label(c["code"], c["loc"]), value=i) for i, c in enumerate(d["cards"])]
        what = "order these effects on the chain (first = lowest Chain Link)" if kind == "sort_chain" else "order these cards (first = top)"
        decision = Decision(kind, p, f"{who}: {what}", opts, pick=(len(opts), len(opts)))
        if kind == "sort_card" or len(opts) < 2:
            decision.auto = bytes([0xFF])  # keep the engine's order
        return decision

    def _ask_sort_card(self, d, p, who, hint) -> Decision:
        return self._ask_sort_chain(d, p, who, hint, "sort_card")

    def _ask_announce_race(self, d, p, who, hint) -> Decision:
        opts = [Option(self.cards.system.get(1020 + i, f"Type {i}"), value=1 << i) for i in range(64) if d["available"] >> i & 1]
        return Decision("race", p, f"{who}: declare {d['count']} Monster Type(s)", opts, pick=(d["count"], d["count"]))

    def _ask_announce_attrib(self, d, p, who, hint) -> Decision:
        opts = [Option(self.cards.system.get(1010 + i, f"Attribute {i}"), value=1 << i) for i in range(32) if d["available"] >> i & 1]
        return Decision("attribute", p, f"{who}: declare {d['count']} Attribute(s)", opts, pick=(d["count"], d["count"]))

    def _ask_announce_card(self, d, p, who, hint) -> Decision:
        return Decision("announce_card", p, f"{who}: declare a card name", [])

    def _ask_announce_number(self, d, p, who, hint) -> Decision:
        opts = [Option(str(n), _i32(i)) for i, n in enumerate(d["options"])]
        return Decision("number", p, f"{who}: {hint or 'declare a number'}", opts, auto=_i32(0) if len(opts) == 1 else None)

    def _ask_rock_paper_scissors(self, d, p, who, hint) -> Decision:
        return Decision(
            "rps",
            p,
            f"{who}: rock paper scissors",
            [Option("Rock", _i32(1)), Option("Paper", _i32(2)), Option("Scissors", _i32(3))],
            auto=_i32(1),
        )

    # ------------------------------------------------------------------ log
    def _record(self, m: Msg) -> None:
        d, say = m.data, self.log.append
        if m.name == "MOVE" and d["reason"] & REASON_RELEASE and d["reason"] & REASON_SUMMON:
            self._tributed = True  # consumed by the SUMMONING or SET that follows
        match m.name:
            case "NEW_TURN":
                self.turn += 1
                self.turn_player = d["player"]
                say(f"— Turn {self.turn}: P{d['player'] + 1}'s turn —")
            case "NEW_PHASE":
                self.phase = d["phase"]
                say(f"P{self.turn_player + 1} enters the {PHASES.get(d['phase'], f'phase {d["phase"]:#x}')}")
            case "HINT":
                self._hint(d)
            case "MOVE":
                self._move(d)
            case "SUMMONING":
                kind = "Tribute Summons" if self._tributed else "Normal Summons"
                self._tributed = False
                say(f"P{d['loc'].controller + 1} {kind} {self.label(d['code'], d['loc'])}")
            case "SPSUMMONING":
                say(
                    f"{self.name(d['code'])} is Special Summoned to {self.where(d['loc'])} in {POSITIONS.get(d['loc'].position, 'some')} Position"
                )
            case "FLIPSUMMONING":
                say(f"P{d['loc'].controller + 1} Flip Summons {self.label(d['code'], d['loc'])}")
            case "SUMMONED" | "SPSUMMONED" | "FLIPSUMMONED":
                say("  (summon successful)")
            case "SET":
                self._tributed = False
                say(f"P{d['loc'].controller + 1} Sets {self.label(d['code'], d['loc'])}")
            case "POS_CHANGE":
                say(f"{self.label(d['code'], d['loc'])} changes to {POSITIONS.get(d['to'], 'another')} Position")
            case "SWAP":
                say(f"{self.name(d['code'])} and {self.name(d['code2'])} swap control")
            case "CHAINING":
                self.chain.append(d)
                source = Loc(d["player"], d["from_location"], d["from_sequence"])
                say(
                    f"Chain Link {d['chain']}: P{d['player'] + 1} activates {self.label(d['code'], source)}{self.effect(d['desc'], d['code'])}"
                )
            case "CHAIN_SOLVING":
                say(f"Resolving Chain Link {d['chain']} ({self._link_name(d['chain'])})")
            case "CHAIN_NEGATED":
                say(f"Chain Link {d['chain']} ({self._link_name(d['chain'])}): its activation is negated")
            case "CHAIN_DISABLED":
                say(f"Chain Link {d['chain']} ({self._link_name(d['chain'])}): its effect is negated")
            case "CHAIN_END":
                self.chain.clear()
                say("The chain has resolved.")
            case "BECOME_TARGET":
                say("Targets: " + ", ".join(self.at(loc) for loc in d["cards"]))
            case "CARD_SELECTED":
                say("Selected: " + ", ".join(self.at(loc) for loc in d["cards"]))
            case "RANDOM_SELECTED":
                say("Randomly selected: " + ", ".join(self.at(loc) for loc in d["cards"]))
            case "DRAW":
                say(f"P{d['player'] + 1} draws " + ", ".join(self.name(c["code"]) for c in d["cards"]))
            case "DAMAGE" | "PAY_LPCOST":
                self.lp[d["player"]] -= d["amount"]
                verb = "takes" if m.name == "DAMAGE" else "pays"
                say(f"P{d['player'] + 1} {verb} {d['amount']} {'damage' if verb == 'takes' else 'LP'} (LP {self.lp[d['player']]})")
            case "RECOVER":
                self.lp[d["player"]] += d["amount"]
                say(f"P{d['player'] + 1} gains {d['amount']} LP (LP {self.lp[d['player']]})")
            case "LPUPDATE":
                self.lp[d["player"]] = d["amount"]
                say(f"P{d['player'] + 1}'s LP becomes {d['amount']}")
            case "EQUIP":
                say(f"{self.at(d['card'])} is equipped to {self.at(d['target'])}")
            case "CARD_TARGET":
                say(f"{self.at(d['card'])} now targets {self.at(d['target'])}")
            case "CANCEL_TARGET":
                say(f"{self.at(d['card'])} no longer targets {self.at(d['target'])}")
            case "ADD_COUNTER" | "REMOVE_COUNTER":
                counter = self.cards.counters.get(d["counter"], "counter")
                verb = "placed on" if m.name == "ADD_COUNTER" else "removed from"
                say(f"{d['count']} {counter}(s) {verb} {self.at(d['loc'])}")
            case "ATTACK":
                target = self.at(d["target"]) if d["target"].location else "directly"
                say(f"{self.at(d['attacker'])} attacks {target}")
            case "BATTLE":
                line = f"Damage calculation: {self.at(d['attacker'])} ATK {d['atk']}"
                if d["target"].location:
                    line += f" vs {self.at(d['target'])} ATK {d['target_atk']} / DEF {d['target_def']}"
                say(line)
            case "ATTACK_DISABLED":
                say("The attack is negated")
            case "MISSED_EFFECT":
                say(f"{self.label(d['code'], d['loc'])}: its effect misses the timing")
            case "TOSS_COIN":
                say(f"Coin toss for P{d['player'] + 1}: " + ", ".join("Heads" if r else "Tails" for r in d["results"]))
            case "TOSS_DICE":
                say(f"Die roll for P{d['player'] + 1}: " + ", ".join(map(str, d["results"])))
            case "CONFIRM_CARDS":
                say(f"Revealed to P{d['player'] + 1}: " + ", ".join(self.name(c["code"]) for c in d["cards"]))
            case "CONFIRM_DECKTOP" | "CONFIRM_EXTRATOP":
                say(f"Revealed from the top of P{d['player'] + 1}'s Deck: " + ", ".join(self.name(c["code"]) for c in d["cards"]))
            case "WIN":
                reason = self.cards.victory.get(d["reason"], "")
                self.result = "It's a draw." if d["player"] > 1 else f"P{d['player'] + 1} wins{f' ({reason})' if reason else ''}."
                say(self.result)

    def _link_name(self, number: int) -> str:
        return self.name(self.chain[number - 1]["code"]) if 0 < number <= len(self.chain) else "?"

    def _hint(self, d: dict) -> None:
        kind, player, value = d["type"], d["player"], d["value"]
        if kind == HINT_SELECTMSG:
            self.select_hint[player] = value
        elif kind == HINT_EVENT:
            self.event_hint = self.effect_text(value)
        elif kind == HINT_OPSELECTED and value:
            self.log.append(f"Chosen: {self.effect_text(value) or value}")
        elif kind == HINT_MESSAGE:
            self.log.append(f"Message: {self.effect_text(value)}")
        elif kind == HINT_CODE:
            self.log.append(f"P{player + 1} declares the card name {self.name(value)}")
        elif kind == HINT_NUMBER:
            self.log.append(f"P{player + 1} declares {value}")
        elif kind == HINT_RACE:
            self.log.append(f"P{player + 1} declares {', '.join(self.cards.bits(value, 1020))}")
        elif kind == HINT_ATTRIB:
            self.log.append(f"P{player + 1} declares {', '.join(self.cards.bits(value, 1010))}")
        elif kind == HINT_CARD and value:
            self.log.append(f"({self.name(value)}'s effect applies)")

    def _move(self, d: dict) -> None:
        src, dst, reason = d["from"], d["to"], d["reason"]
        name = self.name(d["code"])
        if dst.location in (E.LOCATION_MZONE, E.LOCATION_SZONE) and (reason & (REASON_SUMMON | REASON_SPSUMMON) or not reason):
            return  # summons and card activations get their own lines
        if not dst.location:
            self.log.append(f"{name} ({self.where(src)}) leaves the duel")
            return
        if dst.location & E.LOCATION_OVERLAY:
            self.log.append(
                f"{name} ({self.where(src)}) becomes Xyz Material of the monster in {self.where(Loc(dst.controller, E.LOCATION_MZONE, dst.sequence))}"
            )
            return
        if reason & REASON_DRAW:
            return  # the DRAW message says it
        how = []
        if reason & REASON_DESTROY:
            how.append(
                "destroyed by battle" if reason & REASON_BATTLE else "destroyed by an effect" if reason & REASON_EFFECT else "destroyed"
            )
        if reason & REASON_RELEASE:
            how.append("Tributed")
        if reason & REASON_DISCARD:
            how.append("discarded")
        if reason & REASON_MATERIAL:
            kinds = [k for bit, k in MATERIAL_KINDS.items() if reason & bit]
            how.append(f"used as {'/'.join(kinds) or 'a'} Material")
        if reason & REASON_COST:
            how.append("as a cost")
        elif reason & REASON_EFFECT and not reason & REASON_DESTROY:
            how.append("by an effect")
        if reason & REASON_RULE:
            how.append("by rule")
        facedown = " (face-down)" if dst.location == E.LOCATION_REMOVED and dst.position & E.POS_FACEDOWN else ""
        self.log.append(f"{name}: {self.where(src)} → {self.where(dst)}{facedown}{' — ' + ', '.join(how) if how else ''}")

    # ------------------------------------------------------------------ board
    def board(self) -> str:
        lines = [f"Turn {self.turn}, P{self.turn_player + 1}'s {PHASES.get(self.phase, 'turn')}"]
        chain = parse_chain(self.engine.query_field())
        if chain:
            lines.append("Current chain: " + "; ".join(f"CL{i + 1} {self.name(code)}" for i, code in enumerate(chain)))
        for p in (0, 1):
            zones = {
                loc: parse_location(self.engine.query_location(p, loc, BOARD_QUERY))
                for loc in (
                    E.LOCATION_MZONE,
                    E.LOCATION_SZONE,
                    E.LOCATION_HAND,
                    E.LOCATION_GRAVE,
                    E.LOCATION_REMOVED,
                    E.LOCATION_EXTRA,
                    E.LOCATION_DECK,
                )
            }
            count = {loc: sum(1 for c in cards if c) for loc, cards in zones.items()}
            lines.append(
                f"P{p + 1}: LP {self.lp[p]} · hand {count[E.LOCATION_HAND]} · Deck {count[E.LOCATION_DECK]} · "
                f"Extra Deck {count[E.LOCATION_EXTRA]} · GY {count[E.LOCATION_GRAVE]} · banished {count[E.LOCATION_REMOVED]}"
            )
            for loc in (E.LOCATION_MZONE, E.LOCATION_SZONE):
                for seq, info in enumerate(zones[loc]):
                    if info:
                        lines.append(f"  {self.where(Loc(p, loc, seq))}: {self._card_state(info, loc)}")
            for loc, title in (
                (E.LOCATION_HAND, "Hand"),
                (E.LOCATION_GRAVE, "GY"),
                (E.LOCATION_REMOVED, "Banished"),
                (E.LOCATION_EXTRA, "Extra Deck"),
                (E.LOCATION_DECK, "Deck (top first)"),
            ):
                cards = [c for c in zones[loc] if c]
                if loc == E.LOCATION_DECK:
                    cards.reverse()
                if cards:
                    names = [
                        self.name(c.get("code", 0))
                        + (" (face-down)" if loc == E.LOCATION_REMOVED and c.get("position", 0) & E.POS_FACEDOWN else "")
                        for c in cards
                    ]
                    more = f" … (+{len(names) - 15} more)" if len(names) > 15 else ""
                    lines.append(f"  {title}: {', '.join(names[:15])}{more}")
        return "\n".join(lines)

    def _card_state(self, info: dict, location: int) -> str:
        code, pos = info.get("code", 0), info.get("position", 0)
        parts = [
            self.name(code),
            POSITIONS.get(pos, "") if location == E.LOCATION_MZONE else ("Set" if pos & E.POS_FACEDOWN else "face-up"),
        ]
        if location == E.LOCATION_MZONE and pos & E.POS_FACEUP:
            card = self.cards.get(code)
            if info.get("link"):
                parts.append(
                    f"ATK {info['attack']}" + (f" (base {info['base_attack']})" if info["attack"] != info.get("base_attack") else "")
                )
            elif card is not None:
                parts.append(
                    f"ATK {info['attack']}/DEF {info['defense']}"
                    + (
                        f" (base {info['base_attack']}/{info['base_defense']})"
                        if (info["attack"], info["defense"]) != (info.get("base_attack"), info.get("base_defense"))
                        else ""
                    )
                )
        if info.get("status", 0) & STATUS_DISABLED:
            parts.append("effects negated")
        if location == E.LOCATION_SZONE and info.get("status", 0) & STATUS_SET_TURN:
            parts.append("Set this turn")
        if info.get("materials"):
            parts.append("materials: " + ", ".join(self.name(c) for c in info["materials"]))
        if info.get("counters"):
            parts.append(", ".join(f"{n} {self.cards.counters.get(t, 'counter')}" for t, n in info["counters"]))
        if info.get("equip"):
            parts.append(f"equipped to the card in {self.where(info['equip'])}")
        return ", ".join(p for p in parts if p)

    # ------------------------------------------------------------------ output
    def header(self) -> str:
        phase = PHASES.get(self.phase, "")
        return f"Duel {self.id} · Turn {self.turn}, P{self.turn_player + 1}'s {phase} · LP P1 {self.lp[0]} / P2 {self.lp[1]}"

    def describe_decision(self) -> str:
        d = self.decision
        if d is None:
            return f"The duel is over. {self.result}".strip()
        lines = [f"Decision for P{d.player + 1}: {d.title}"]
        lines += [f"  [{i}] {o.label}" for i, o in enumerate(d.options)]
        if d.kind == "announce_card":
            lines.append("Answer with the card name to declare.")
        elif d.kind in ("sort_chain", "sort_card"):
            lines.append("Answer with a list of all options in the order you want.")
        elif d.kind == "counter":
            lines.append("Answer with a list naming a card once per counter to remove, e.g. [0, 0, 1].")
        elif d.kind == "unselect":
            lines.append("Answer with one option, or a list to pick several in a row (Finish is sent after the list).")
        elif d.pick:
            low, high = d.pick
            count = f"{low}" if low == high else f"{low} to {high}"
            lines.append(f"Pick {count} option(s): a number, text, or a list like [0, 2]." + (' "cancel" is allowed.' if d.cancel else ""))
        return "\n".join(lines)

    def render(self, log_start: int, errors_start: int, notes: list[str], full_log: bool = False) -> str:
        out = [self.header()]
        log = self.log if full_log else self.log[log_start:]
        if log:
            out.append("What happened:" if not full_log else "Log:")
            out += [f"  {line}" for line in log]
        out += notes
        new_errors = self.errors[errors_start:]
        if new_errors:
            out.append("Engine/script errors (the result may be wrong): " + " | ".join(new_errors[-5:]))
        out.append(self.describe_decision())
        return "\n".join(out)


# ---------------------------------------------------------------------- query parsing
def _read_info(buf: bytes, pos: int) -> tuple[dict, int]:
    """One card's query block (u16 size, u32 flag, payload... until QUERY_END)."""
    info: dict = {}
    while pos + 6 <= len(buf):
        size, flag = struct.unpack_from("<HI", buf, pos)
        data = buf[pos + 6 : pos + 2 + size]
        pos += 2 + size
        if flag == E.QUERY_END:
            break
        if flag == E.QUERY_CODE:
            info["code"] = struct.unpack_from("<I", data)[0]
        elif flag == E.QUERY_POSITION:
            info["position"] = struct.unpack_from("<I", data)[0]
        elif flag in (E.QUERY_ATTACK, E.QUERY_DEFENSE, E.QUERY_BASE_ATTACK, E.QUERY_BASE_DEFENSE):
            key = {
                E.QUERY_ATTACK: "attack",
                E.QUERY_DEFENSE: "defense",
                E.QUERY_BASE_ATTACK: "base_attack",
                E.QUERY_BASE_DEFENSE: "base_defense",
            }[flag]
            info[key] = struct.unpack_from("<i", data)[0]
        elif flag == E.QUERY_STATUS:
            info["status"] = struct.unpack_from("<I", data)[0]
        elif flag == E.QUERY_OWNER:
            info["owner"] = data[0]
        elif flag == E.QUERY_LINK:
            info["link"], info["link_marker"] = struct.unpack_from("<II", data)
        elif flag == E.QUERY_OVERLAY_CARD:
            count = struct.unpack_from("<I", data)[0]
            info["materials"] = list(struct.unpack_from(f"<{count}I", data, 4))
        elif flag == E.QUERY_COUNTERS:
            count = struct.unpack_from("<I", data)[0]
            info["counters"] = [(v & 0xFFFF, v >> 16) for v in struct.unpack_from(f"<{count}I", data, 4)]
        elif flag == E.QUERY_EQUIP_CARD and len(data) >= 10 and data[1]:
            info["equip"] = Loc(data[0], data[1], struct.unpack_from("<I", data, 2)[0])
    return info, pos


def parse_card(buf: bytes) -> dict | None:
    return _read_info(buf, 0)[0] if buf else None


def parse_location(buf: bytes) -> list[dict | None]:
    """OCG_DuelQueryLocation result: u32 size, then per slot an empty u16 or a card block."""
    cards, pos = [], 4
    while pos + 2 <= len(buf):
        if struct.unpack_from("<H", buf, pos)[0] == 0:
            cards.append(None)
            pos += 2
            continue
        info, pos = _read_info(buf, pos)
        cards.append(info)
    return cards


def parse_chain(buf: bytes) -> list[int]:
    """Card codes on the current chain, from OCG_DuelQueryField."""
    if not buf:
        return []
    pos = 4
    for _ in range(2):
        pos += 4  # LP
        for _ in range(7 + 8):  # monster zones, then spell & trap zones
            occupied = buf[pos]
            pos += 6 if occupied else 1
        pos += 6 * 4  # deck, hand, GY, banished, Extra Deck, face-up Extra Deck counts
    count = struct.unpack_from("<I", buf, pos)[0]
    pos += 4
    codes = []
    for _ in range(count):
        codes.append(struct.unpack_from("<I", buf, pos)[0])
        pos += 4 + 10 + 6 + 8
    return codes
