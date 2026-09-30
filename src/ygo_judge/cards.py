"""Card data (BabelCDB .cdb files), card scripts (CardScripts) and EDOPro's strings.conf."""

import ctypes as C
import difflib
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .engine import ROOT, CardData

DATA = Path(os.environ.get("YGO_DATA", ROOT / "vendor"))

TYPE_MONSTER, TYPE_SPELL, TYPE_TRAP = 0x1, 0x2, 0x4
TYPE_NORMAL, TYPE_EFFECT, TYPE_FUSION, TYPE_RITUAL = 0x10, 0x20, 0x40, 0x80
TYPE_SYNCHRO, TYPE_TOKEN, TYPE_XYZ, TYPE_PENDULUM, TYPE_LINK = 0x2000, 0x4000, 0x800000, 0x1000000, 0x4000000
EXTRA_DECK_TYPES = TYPE_FUSION | TYPE_SYNCHRO | TYPE_XYZ | TYPE_LINK

# Link arrows, bit order of LINK_MARKER_* in constant.lua.
ARROWS = ["Bottom-Left", "Bottom", "Bottom-Right", "Left", "", "Right", "Top-Left", "Top", "Top-Right"]

# Databases with cards that play under normal (Master Rule) duels; Rush, Skills and anime cards stay out.
CDB_PATTERN = re.compile(r"^(cards|release-.*|prerelease-(?!cards-rush).*)\.cdb$")
# Script folders in lookup order; library scripts (utility.lua, proc_*.lua, ...) live at the root.
SCRIPT_FOLDERS = ["", "official", "pre-release", "pre-errata", "unofficial", "goat", "skill", "rush"]


@dataclass(frozen=True)
class Card:
    code: int
    alias: int
    name: str
    text: str
    strings: tuple[str, ...]
    type: int
    level: int
    lscale: int
    rscale: int
    attribute: int
    race: int
    atk: int
    defense: int
    link_marker: int
    setcodes: tuple[int, ...]

    def is_(self, type_bits: int) -> bool:
        return bool(self.type & type_bits)


def normalize(name: str) -> str:
    name = name.lower().replace("’", "'").replace("–", "-").replace("—", "-")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", name).split())


class CardDB:
    def __init__(self, data_dir: Path = DATA):
        self.scripts = data_dir / "CardScripts"
        self.cards: dict[int, Card] = {}
        for path in sorted((data_dir / "BabelCDB").glob("*.cdb")):
            if CDB_PATTERN.match(path.name):
                self._load(path)
        # Name search skips alternate artworks (rows aliasing a card with the same name).
        self.by_name: dict[str, list[Card]] = {}
        for card in self.cards.values():
            original = self.cards.get(card.alias)
            if original and original.name == card.name:
                continue
            self.by_name.setdefault(normalize(card.name), []).append(card)
        self.system, self.counters, self.victory = self._load_strings(data_dir / "strings.conf")
        self.counter_by_name = {normalize(v): k for k, v in self.counters.items()}
        self._setcode_buffers: dict[int, C.Array] = {}

    def _load(self, path: Path) -> None:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        rows = db.execute(
            "select d.id, d.alias, d.setcode, d.type, d.atk, d.def, d.level, d.race, d.attribute,"
            " t.name, t.desc, " + ", ".join(f"t.str{i}" for i in range(1, 17)) + " from datas d join texts t on t.id = d.id"
        )
        for code, alias, setcode, type_, atk, def_, level, race, attribute, name, desc, *strings in rows:
            setcode &= 0xFFFFFFFFFFFFFFFF
            link = bool(type_ & TYPE_LINK)
            self.cards[code] = Card(
                code=code,
                alias=alias,
                name=name or str(code),
                text=desc or "",
                strings=tuple(s or "" for s in strings),
                type=type_,
                level=level & 0xFF,
                lscale=(level >> 24) & 0xFF,
                rscale=(level >> 16) & 0xFF,
                attribute=attribute,
                race=race,
                atk=atk,
                defense=0 if link else def_,
                link_marker=def_ if link else 0,
                setcodes=tuple(s for s in ((setcode >> (16 * i)) & 0xFFFF for i in range(4)) if s),
            )
        db.close()

    @staticmethod
    def _load_strings(path: Path) -> tuple[dict[int, str], dict[int, str], dict[int, str]]:
        system, counters, victory = {}, {}, {}
        for line in path.read_text(encoding="utf-8").splitlines():
            kind, _, rest = line.partition(" ")
            key, _, value = rest.partition(" ")
            target = {"!system": system, "!counter": counters, "!victory": victory}.get(kind)
            if target is not None and key:
                target[int(key, 0)] = value.strip()
        return system, counters, victory

    # ------------------------------------------------------------------ engine callbacks
    def fill(self, code: int, data: CardData) -> None:
        """Card reader for the engine: copies a card's stats into `data`."""
        data.code = code
        card = self.cards.get(code)
        if card is None:
            return
        buf = self._setcode_buffers.get(code)
        if buf is None:  # zero-terminated; kept alive because the engine copies it lazily
            buf = self._setcode_buffers[code] = (C.c_uint16 * (len(card.setcodes) + 1))(*card.setcodes, 0)
        data.alias = card.alias
        data.setcodes = C.cast(buf, C.POINTER(C.c_uint16))
        data.type = card.type
        data.level = card.level
        data.attribute = card.attribute
        data.race = card.race
        data.attack = card.atk
        data.defense = card.defense
        data.lscale = card.lscale
        data.rscale = card.rscale
        data.link_marker = card.link_marker

    def script(self, name: str) -> bytes | None:
        """Script reader for the engine: finds `c<code>.lua` or a library script by file name."""
        name = os.path.basename(name)
        for folder in SCRIPT_FOLDERS:
            path = self.scripts / folder / name
            if path.is_file():
                return path.read_bytes()
        return None

    # ------------------------------------------------------------------ lookup
    def get(self, code: int) -> Card | None:
        return self.cards.get(code)

    def name(self, code: int) -> str:
        card = self.cards.get(code)
        return card.name if card else f"card #{code}"

    def search(self, query: str, limit: int = 5) -> list[Card]:
        query = query.strip()
        if query.isdigit():
            card = self.cards.get(int(query))
            return [card] if card else []
        key = normalize(query)
        if key in self.by_name:
            return self.by_name[key][:limit]
        names = [n for n in self.by_name if key in n]
        names.sort(key=lambda n: (len(n) - len(key), n))
        if len(names) < limit:
            close = difflib.get_close_matches(key, self.by_name.keys(), n=limit, cutoff=0.6)
            names += [n for n in close if n not in names]
        return [self.by_name[n][0] for n in names[:limit]]

    def resolve(self, ref: str | int) -> Card:
        """A card from its passcode or exact name; raises with suggestions otherwise."""
        if isinstance(ref, int) or str(ref).strip().isdigit():
            card = self.cards.get(int(ref))
            if card is None:
                raise ValueError(f"No card with passcode {ref}.")
            return card
        key = normalize(ref)
        if key in self.by_name:
            return self.by_name[key][0]
        partial = [n for n in self.by_name if key and key in n]
        if len(partial) == 1:  # an unambiguous part of a name, never a fuzzy guess
            return self.by_name[partial[0]][0]
        hint = "; ".join(f"{c.name} ({c.code})" for c in self.search(ref, 5)) or "nothing similar"
        raise ValueError(f'Unknown card "{ref}". Did you mean: {hint}? Use find_card to get the exact name.')

    # ------------------------------------------------------------------ text
    def desc(self, desc: int) -> str:
        """Text of an effect description: aux.Stringid(code, i) or a system string."""
        code, index = desc >> 20, desc & 0xFFFFF
        if code:
            card = self.cards.get(code)
            return card.strings[index] if card and index < 16 else ""
        return self.system.get(desc, "")

    def bits(self, value: int, first_string: int) -> list[str]:
        return [self.system.get(first_string + i, f"#{1 << i}") for i in range(64) if value >> i & 1]

    def type_line(self, card: Card) -> str:
        types = [self.system.get(1050 + i, "") for i in range(32) if card.type >> i & 1]
        types = [t for t in types if t and t != "Special Summon"]
        if not card.is_(TYPE_MONSTER):
            kind = "Spell" if card.is_(TYPE_SPELL) else "Trap"
            sub = [t for t in types if t not in (kind,)]
            return f"{' '.join(sub) + ' ' if sub else 'Normal '}{kind}"
        parts = ["/".join(t for t in types if t != "Monster") + " Monster"]
        if card.is_(TYPE_LINK):
            parts.append(f"Link-{card.level}")
        elif card.is_(TYPE_XYZ):
            parts.append(f"Rank {card.level}")
        else:
            parts.append(f"Level {card.level}")
        parts.append(" ".join(self.bits(card.attribute, 1010) + self.bits(card.race, 1020)))
        if card.is_(TYPE_PENDULUM):
            parts.append(f"Scale {card.lscale}")
        stats = f"ATK {card.atk if card.atk >= 0 else '?'}"
        if card.is_(TYPE_LINK):
            arrows = [a for i, a in enumerate(ARROWS) if card.link_marker >> i & 1 and a]
            stats += f", arrows {', '.join(arrows)}"
        else:
            stats += f" / DEF {card.defense if card.defense >= 0 else '?'}"
        parts.append(stats)
        return " · ".join(parts)

    def describe(self, card: Card) -> str:
        return f"{card.name} [{card.code}]\n{self.type_line(card)}\n{card.text}"
