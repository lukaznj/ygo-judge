"""MCP server: EDOPro's rules engine as tools for testing Yu-Gi-Oh! rulings."""

import functools
import json
import os
import re
import secrets
import threading
from collections import OrderedDict
from pathlib import Path

from mcp.server import MCPServer
from mcp_types import ToolAnnotations
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

from .cards import CardDB
from .compatibility import simulation_fingerprint
from .duel import ChoiceError, Duel
from .engine import ROOT, EngineError
from .rulings import rulings
from .scenario import Setup

STATE = Path(os.environ.get("STATE_DIR", ROOT / "state"))
MAX_LIVE_DUELS = 64
DUEL_RULES_VERSION = 3  # Normal draws/shuffles, explicit decisions and summon history.


def _setting(name: str) -> str:
    """$NAME, else the contents of STATE_DIR/<name> (files can change without a restart)."""
    file = STATE / name.lower()
    return os.environ.get(name) or (file.read_text().strip() if file.exists() else "")


def public_url() -> str:
    """Base URL for puzzle download links."""
    return _setting("PUBLIC_URL").rstrip("/")


INSTRUCTIONS = """\
Test Yu-Gi-Oh! card interactions on EDOPro's rules engine (Project Ignis card scripts). You play both
players, so the engine, not memory, decides what is legal and what happens.
Match the question's turn and phase: turn 1 normally has no Battle Phase or Main Phase 2.
Do not infer that an effect is legal from card text or FAQ alone; reproduce its activation timing.

1. Look up every card with find_card and use the exact names it returns.
2. Build the smallest board that reproduces the situation with start_duel. P1 is the turn player: the
   duel starts on turn 1, stopping at the first legal decision (usually Main Phase 1). It has no
   Battle Phase or Main Phase 2. For going-second questions, put that player's cards in p2 and end
   P1's turn with choose. For P1's next turn, end P2's turn as well. Verify the turn and phase in the returned state; never treat a later-turn test
   as proof for turn 1. Set Spells/Traps can be activated right away. Decks are empty unless
   you list cards. Normal Draw Phase draws apply from turn 2; add deck_filler for later turns
   and effects that draw or mill. Shuffles use the seed. For revival-restricted monsters placed in
   the GY or banished, use {"card": "name", "properly_summoned": true/false} to specify history.
   Do not guess this history. Things that must already have happened this turn (a Normal Summon,
   a used once-per-turn effect) have to be played out.
3. Answer decisions with choose. Every decision lists numbered options for one player; answer with the
   integer index or the option's text (numeric strings match numeric labels). You can queue several
   choices; each is used at the first decision it fits. Only windows with no legal responses and
   forced single choices are skipped.
   Choose zones, positions, card order and rock-paper-scissors explicitly; pass legal responses explicitly.
4. An option that isn't listed is not legal right now: "can X respond here?" is answered by whether
   the engine offers it. The log shows chain links, negations, costs, missed timings and damage.
5. A simulation reporting engine/script errors is invalid evidence; fix the cause and start again.
   Use undo to try the other branch (e.g. with and without a response).
6. Check get_rulings for official Konami Q&As. The scripts follow OCG rulings, and scripts can have
   bugs, so say when the engine and the rulings disagree, or when a TCG ruling may differ.
7. Answer with a short verdict first, then the chain of events from the log, then caveats. Offer
   export_puzzle so the players can replay the exact situation in EDOPro.
"""

cards = CardDB()
mcp = MCPServer("ygo-judge", title="Yu-Gi-Oh! Judge", instructions=INSTRUCTIONS)
READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
PLAY = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)


class Duels:
    """Live duels in memory, plus each duel's setup and responses on disk so it can be rebuilt."""

    def __init__(self):
        self.live: OrderedDict[str, Duel] = OrderedDict()
        self.fingerprint = simulation_fingerprint(cards.data_dir)
        # ponytail: one lock for every duel; per-duel locks if many people play at once
        self.lock = threading.RLock()

    def _path(self, duel_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{8}", duel_id):
            raise ValueError(f"Unknown duel id {duel_id!r}.")
        return STATE / "duels" / f"{duel_id}.json"

    def create(self, setup: Setup) -> Duel:
        duel = Duel(cards, setup, secrets.token_hex(4))
        self._keep(duel)
        return duel

    def get(self, duel_id: str) -> Duel:
        path = self._path(duel_id)
        if duel_id in self.live:
            self.live.move_to_end(duel_id)
            self.live[duel_id].ensure_valid()
            return self.live[duel_id]
        if not path.exists():
            raise ValueError(f"Unknown duel id {duel_id!r}; start a new one with start_duel.")
        data = json.loads(path.read_text())
        if data.get("rules_version", 1) != DUEL_RULES_VERSION:
            raise ValueError(
                "This saved duel uses incompatible simulation rules. "
                "Start a new duel with start_duel and retest the ruling."
            )
        if data.get("simulation_fingerprint") != self.fingerprint:
            raise ValueError("This saved duel uses different engine/card assets. Start a new duel with start_duel and retest the ruling.")
        responses = [(bytes.fromhex(r), chosen) for r, chosen in data["responses"]]
        duel = Duel(cards, Setup.model_validate(data["setup"]), duel_id, responses)
        self._keep(duel)
        return duel

    def save(self, duel: Duel) -> None:
        duel.ensure_valid()
        path = self._path(duel.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        responses = [[r.hex(), chosen] for r, chosen in duel.responses]
        path.write_text(json.dumps({
            "rules_version": DUEL_RULES_VERSION,
            "simulation_fingerprint": self.fingerprint,
            "setup": duel.setup.model_dump(),
            "responses": responses,
        }))

    def _keep(self, duel: Duel) -> None:
        self.live[duel.id] = duel
        while len(self.live) > MAX_LIVE_DUELS:
            self.live.popitem(last=False)


duels = Duels()


def _guard(fn):
    """Setup and choice mistakes come back as text the model can act on."""

    @functools.wraps(fn)
    def run(*args, **kwargs):
        try:
            with duels.lock:
                return fn(*args, **kwargs)
        except (ValueError, ChoiceError, EngineError) as e:
            return f"Error: {e}"

    return run


@mcp.tool(annotations=READ_ONLY, structured_output=False)
def find_card(query: str, limit: int = 5) -> str:
    """Find cards by name (typos and partial names are fine) or passcode. Returns each card's exact
    name, passcode, type, stats and full text as used by the engine."""
    found = cards.search(query, max(1, min(limit, 20)))
    if not found:
        return f'No card matches "{query}".'
    return "\n\n".join(cards.describe(c) for c in found)


@mcp.tool(annotations=PLAY, structured_output=False)
@_guard
def start_duel(setup: Setup) -> str:
    """Build a board and start turn 1 with P1 as the turn player. Stops at the first legal decision,
    usually Main Phase 1, but earlier activation windows are shown too (no Battle Phase or Main Phase 2).
    For going second, put your cards in p2 and use choose to end P1's turn; end P2's turn too to reach P1's next turn. Cards are given by exact name or passcode.
    Returns the duel id, the board and the first decision."""

    duel = duels.create(setup)
    update = duel.play([])
    duels.save(duel)
    return f"Started duel {duel.id}.\n{duel.board()}\n\n{update}"


@mcp.tool(annotations=PLAY, structured_output=False)
@_guard
def choose(duel_id: str, choices: list[int | str | list[int | str]]) -> str:
    """Answer decisions in a duel. Each choice is an option number, (part of) an option's text, or a
    list of those for decisions that pick several things. Choices are used in order, each at the first
    decision it fits. Only forced single choices and response windows with no legal effects are
    handled automatically. Every legal response, zone, position and ordering choice is shown.
    Integer choices are zero-based indices; strings match option text first, including numeric labels.
    Returns what happened and the next decision."""

    duel = duels.get(duel_id)
    update = duel.play(choices)
    duels.save(duel)
    return update


@mcp.tool(annotations=READ_ONLY, structured_output=False)
@_guard
def get_state(duel_id: str) -> str:
    """The full board (zones, ATK/DEF, negated cards, materials, counters), the whole log and the
    pending decision."""

    duel = duels.get(duel_id)
    log = "\n".join(f"  {line}" for line in duel.log)
    return f"{duel.header()}\n{duel.board()}\n\nLog:\n{log}\n\n{duel.describe_decision()}"


@mcp.tool(annotations=PLAY, structured_output=False)
@_guard
def undo(duel_id: str, decisions: int = 1) -> str:
    """Take back the last `decisions` choices and return to that decision, to try another branch."""

    duel = duels.get(duel_id)
    update = duel.undo(decisions)
    duels.save(duel)
    return update


@mcp.tool(annotations=READ_ONLY, structured_output=False)
@_guard
def export_puzzle(duel_id: str) -> str:
    """The duel's starting board as an EDOPro puzzle script, so players can replay it in EDOPro."""

    duel = duels.get(duel_id)
    base = public_url()
    link = f"\nDownload: {base}/puzzle/{duel.id}" if base else ""
    return (
        f"Save this as judge-{duel.id}.lua in EDOPro's puzzles folder, then open it from Puzzles. "
        f"You control both players there too.{link}\n\n```lua\n{duel.puzzle}```"
    )


@mcp.tool(annotations=READ_ONLY, structured_output=False)
def get_rulings(cards: list[str]) -> str:
    """Official rulings from the YGOrg card database: Konami's FAQ for each card, plus the Q&As that
    involve all the given cards when you pass two or more."""
    try:
        return rulings(cards)
    except OSError as e:
        return f"The rulings database is unavailable right now ({e})."
    except (ValueError, KeyError, TypeError, AttributeError) as e:  # unexpected data from the external database
        return f"Couldn't read the rulings database's answer ({type(e).__name__}: {e})."


@mcp.tool(annotations=READ_ONLY, structured_output=False)
def card_script(card: str) -> str:
    """A card's Lua script in the engine: shows exactly how EDOPro implements it (targeting, once per
    turn, "if" vs "when" triggers, costs)."""
    try:
        found = cards.resolve(card)
    except ValueError as e:
        return f"Error: {e}"
    source = cards.script(f"c{found.code}.lua")
    return f"{found.name} [{found.code}]\n```lua\n{source.decode(errors='replace')}\n```" if source else f"{found.name} has no script."


@mcp.prompt(title="Judge a ruling")
def judge(question: str) -> str:
    """Test a Yu-Gi-Oh! ruling question on the engine."""
    return f"Act as a Yu-Gi-Oh! judge. Test this on the engine with the ygo-judge tools, then answer:\n\n{question}"


@mcp.custom_route("/puzzle/{duel_id}", methods=["GET"])
async def puzzle_file(request: Request) -> Response:
    try:
        with duels.lock:
            duel = duels.get(request.path_params["duel_id"])
    except ValueError:
        return PlainTextResponse("Unknown duel", status_code=404)
    headers = {"Content-Disposition": f'attachment; filename="judge-{duel.id}.lua"'}
    return Response(duel.puzzle, media_type="text/plain; charset=utf-8", headers=headers)


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> Response:
    return PlainTextResponse("ok")


def main() -> None:
    # The access key is a secret path segment: the connector URL is <host>/<key>/mcp, and the
    # server answers nothing else under /mcp. Puzzle downloads and /health stay public.
    key = _setting("ACCESS_KEY")
    if not key:
        key = secrets.token_urlsafe(18)
        STATE.mkdir(parents=True, exist_ok=True)
        (STATE / "access_key").write_text(key + "\n")
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,}", key):
        raise SystemExit("ACCESS_KEY must be at least 8 characters of A-Z, a-z, 0-9, _ or -.")
    print(f"MCP server URL (ChatGPT or Claude): {public_url() or 'https://<your host>'}/{key}/mcp", flush=True)
    mcp.run(
        transport="streamable-http",
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
        streamable_http_path=f"/{key}/mcp",
        stateless_http=True,
        json_response=True,
    )
