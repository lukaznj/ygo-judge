"""Official rulings from the YGOrg card database (db.ygoresources.com): translated Konami FAQ
entries and Q&As. Responses are cached on disk, as the maintainers ask ("only query data as you
need it, and cache that data locally")."""

import difflib
import json
import os
import re
import time
import urllib.request
from pathlib import Path

from .cards import normalize
from .engine import ROOT

BASE = "https://db.ygoresources.com"
CACHE = Path(os.environ.get("STATE_DIR", ROOT / "state")) / "ygorg"
DAY = 86400
MAX_QAS = 8


def _get(path: str, max_age: int) -> dict:
    # ponytail: fixed cache ages; switch to the /manifest/<revision> endpoint if entries go stale
    file = CACHE / (path.strip("/").replace("/", "_") + ".json")
    if file.exists() and time.time() - file.stat().st_mtime < max_age:
        return json.loads(file.read_text())
    request = urllib.request.Request(BASE + path, headers={"User-Agent": "ygo-judge"})
    with urllib.request.urlopen(request, timeout=15) as response:
        data = json.load(response)
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps(data))
    return data


def _names() -> dict[str, list[int]]:
    return _get("/data/idx/card/name/en", DAY)


def card_id(name: str) -> tuple[int, str] | None:
    index = _names()
    by_key = {normalize(n): (ids[0], n) for n, ids in index.items() if ids}
    key = normalize(name)
    if key in by_key:
        return by_key[key]
    close = difflib.get_close_matches(key, by_key.keys(), n=1, cutoff=0.85)
    return by_key[close[0]] if close else None


def _section(key: str) -> tuple:
    """Sort key for FAQ sections, numbered "0", "1", "1.5", ..."""
    try:
        return (0, float(key), "")
    except ValueError:
        return (1, 0.0, key)


def _text(entry: dict) -> str:
    return entry.get("en") or entry.get("ja") or ""


def _fill_names(text: str, names: dict[int, str]) -> str:
    return re.sub(r"<<(\d+)>>", lambda m: f'"{names.get(int(m[1]), "card " + m[1])}"', text)


def rulings(cards: list[str]) -> str:
    """FAQ entries for each card and the Q&As that involve all of them (two or more cards)."""
    names = {ids[0]: n for n, ids in _names().items() if ids}
    found, out = [], []
    for name in cards:
        hit = card_id(name)
        if hit is None:
            out.append(f'"{name}": not found in the rulings database.')
            continue
        cid, official = hit
        data = _get(f"/data/card/{cid}", 7 * DAY)
        found.append((official, data))
        faq = data.get("faqData") or {}
        lines = [f"== {official} — FAQ (Konami, translated) =="]
        for _, entries in sorted((faq.get("entries") or {}).items(), key=lambda kv: _section(kv[0])):
            lines += [f"- {_fill_names(_text(e), names)}" for e in entries if _text(e)]
        if len(lines) == 1:
            lines.append("- (no FAQ entries)")
        lines.append(f"{len(data.get('qaIndex') or [])} Q&A(s) mention this card.")
        out.append("\n".join(lines))
    if len(found) >= 2:
        shared = set(found[0][1].get("qaIndex") or [])
        for _, data in found[1:]:
            shared &= set(data.get("qaIndex") or [])
        titles = " + ".join(n for n, _ in found)
        if not shared:
            out.append(f"== Q&As involving {titles} ==\nNone found.")
        else:
            picked = sorted(shared, reverse=True)[:MAX_QAS]
            lines = [f"== Q&As involving {titles} ({len(shared)} found, newest {len(picked)} shown) =="]
            for qid in picked:
                qa = _get(f"/data/qa/{qid}", 7 * DAY).get("qaData") or {}
                entry = qa.get("en") or qa.get("ja") or {}
                source = "" if "en" in qa else " (Japanese only)"
                date = (entry.get("thisSrc") or {}).get("date", "")
                lines.append(
                    f"Q{qid}{source} {date}\nQ: {_fill_names(entry.get('question', ''), names)}\nA: {_fill_names(entry.get('answer', ''), names)}"
                )
            out.append("\n\n".join(lines))
    elif len(cards) == 1 and found:
        out.append("Pass two or more cards to see the official Q&As that involve all of them.")
    return "\n\n".join(out) + "\n\nSource: YGOrg card database (db.ygoresources.com), OCG rulings translated from Konami."
