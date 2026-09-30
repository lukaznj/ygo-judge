# YGO Judge

Settle Yu-Gi-Oh! ruling questions by asking Claude. YGO Judge is an [MCP](https://modelcontextprotocol.io)
server that lets Claude use EDOPro's rules engine. Claude builds the board, plays both players,
answers from what the engine actually does, and checks Konami's official Q&As.

> **You:** My opponent activates Reinforcement of the Army. I chain Ash Blossom, and they chain Called
> by the Grave on my Ash. Does the search still happen?

Claude sets up that board and plays it out. This is the log the engine produces:

```
Chain Link 1: P1 activates Reinforcement of the Army (P1 Spell & Trap Zone 1)
Chain Link 2: P2 activates Ash Blossom & Joyous Spring (P2 hand): "Negate that effect"
Ash Blossom & Joyous Spring: P2 hand → P2 GY — discarded, as a cost
Chain Link 3: P1 activates Called by the Grave (P1 Spell & Trap Zone 2)
Targets: Ash Blossom & Joyous Spring (P2 GY)
Resolving Chain Link 3 (Called by the Grave)
Ash Blossom & Joyous Spring: P2 GY → P2 banished — by an effect
Resolving Chain Link 2 (Ash Blossom & Joyous Spring)
Chain Link 2 (Ash Blossom & Joyous Spring): its effect is negated
Resolving Chain Link 1 (Reinforcement of the Army)
Marauding Captain: P1 Deck → P1 hand — by an effect
```

Claude then answers with the verdict and the reasoning, and can hand you the board as an EDOPro
puzzle file so you can replay it yourself.

## How it works

- **The engine** is [EDOPro](https://projectignis.github.io)'s core
  ([edo9300/ygopro-core](https://github.com/edo9300/ygopro-core)), the C++ library that runs every
  EDOPro duel. It uses the [Project Ignis card scripts](https://github.com/ProjectIgnis/CardScripts)
  and [card databases](https://github.com/ProjectIgnis/BabelCDB). It runs headless: no EDOPro
  window, lobby or decks.
- **The harness** talks to the engine through `ctypes`. Every engine prompt becomes a numbered list
  of options for the player who has to decide, and Claude answers by number or by option text.
  Response windows where nobody has a relevant effect are skipped, so a test takes a handful of
  calls. The log is written the way a judge would describe the chain.
- **Boards are EDOPro puzzle scripts.** Every scenario Claude builds opens in EDOPro's Puzzle mode,
  where one person controls both players.
- **Duels are saved as their list of answers.** The engine is deterministic, so undo and server
  restarts simply replay them.
- **Rulings** come from the [YGOrg card database](https://db.ygoresources.com): Konami's FAQ
  entries and Q&As, translated, and cached locally.

## Tools

| Tool | What it does |
| --- | --- |
| `find_card` | Looks up cards by (fuzzy) name or passcode, with full text and stats |
| `start_duel` | Builds a board and starts in P1's Main Phase 1. Covers hand, field, GY, banished, Deck, Extra Deck, summon type, Xyz Materials, equips, continuous targets, counters and LP |
| `choose` | Answers decisions by option number or text; several can be queued |
| `get_state` | Board, full log and the pending decision |
| `undo` | Takes back decisions to try the other branch |
| `export_puzzle` | The board as an EDOPro puzzle (`.lua`), with a download link |
| `get_rulings` | Konami's FAQ for each card and the Q&As that involve all of them |
| `card_script` | A card's Lua script, i.e. exactly how the engine implements it |

There's also a `judge` prompt that takes a ruling question.

## Use it in Claude

On claude.ai or Claude Desktop, go to **Settings → Connectors → Add custom connector** and enter
your server's URL:

```
https://<your host>/<access key>/mcp
```

The connector then also works in the Claude iOS and Android apps. Free Claude accounts can add one
custom connector. Then just ask your question.

The access key is a secret part of the URL. Requests without it get a 404, so treat the URL like a
password.

## Self-hosting

The image `ghcr.io/lukaznj/ygo-judge` is built for `linux/arm64` (Raspberry Pi 4/5, Apple silicon);
for x86 servers, build it yourself from the `Containerfile`. It is rebuilt every week with the latest engine, card scripts and databases.
The server needs about 100 MB of RAM.

### Docker Compose or Portainer

[`compose.yml`](compose.yml) works with `docker compose up -d` and as a Portainer stack
(**Stacks → Add stack → Web editor**, paste the file). Set two variables:

| Variable | What it is |
| --- | --- |
| `STATE_DIR` | Host folder for the access key, saved duels and the rulings cache. Use an absolute path in Portainer. |
| `PUBLIC_URL` | The address people reach the server at, e.g. `https://ygo-judge.example.com`. Used for puzzle download links. |

Put a reverse proxy with HTTPS in front of `127.0.0.1:8010`, e.g. Nginx Proxy Manager, Caddy or a
Cloudflare Tunnel. On first start the server creates its access key in `STATE_DIR/access_key` and
prints the connector URL in its log.

Updates are automatic. The stack includes [Watchtower](https://github.com/nicholas-fedor/watchtower),
which checks for a new image every hour and redeploys the judge when there is one. It only touches
containers labelled `com.centurylinklabs.watchtower.enable=true`.

## Development

```sh
scripts/fetch-data.sh    # vendor/: engine source, card scripts, databases, strings.conf
scripts/build-engine.sh  # build/libocgcore.dylib (or .so)
uv run pytest
uv run ygo-judge         # http://127.0.0.1:8000/<access key>/mcp, key in state/access_key
```

| File | What it holds |
| --- | --- |
| `src/ygo_judge/engine.py` | `ctypes` binding to the engine's C API |
| `src/ygo_judge/messages.py` | Decoder for the engine's binary messages |
| `src/ygo_judge/scenario.py` | Board setups, written as EDOPro puzzle scripts |
| `src/ygo_judge/duel.py` | Decisions, automatic answers, the log, the board and undo |
| `src/ygo_judge/cards.py` | Card data, scripts and strings |
| `src/ygo_judge/rulings.py` | YGOrg client with a disk cache |
| `src/ygo_judge/server.py` | MCP tools and HTTP routes |

## Accuracy

The engine is not a judge. Project Ignis scripts follow OCG rulings, TCG rulings sometimes differ,
and scripts can have bugs, especially for new cards. That's why Claude is told to:

- check the official Q&As;
- say when the engine and the rulings disagree;
- pass on any script errors the engine reports.

Things that happened earlier in a turn can't be written into a board setup; they have to be played
out. Examples are a Normal Summon already used, or a once-per-turn effect already activated.

## License

[AGPL-3.0-or-later](LICENSE). The engine and the card scripts this server runs are AGPL-3.0, so
the server is too. If you host a modified version, share its source with its users.

YGO Judge is not affiliated with Konami. Yu-Gi-Oh! is a trademark of Konami and Kazuki Takahashi.
Credit for the engine goes to edo9300 and the YGOPro contributors, for the card scripts and
databases to Project Ignis, and for the rulings translations to the YGOrganization database team.
