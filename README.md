# Snowball

Research that never repeats itself.

Snowball is a research agent that runs non-stop. You give it a few seed topics. It follows what it finds, layer after layer, and builds its own library. It never re-reads a page it has already read, and it never re-researches a fact or topic it already knows. Once a day it posts a digest of only what is new.

The status today is **v0, a fork of the research-wiki loop**. It already runs unattended on our own models, through an OpenAI-compatible proxy. It searches with an Exa key pool, reads Reddit through Arctic Shift, keeps a markdown wiki, guards against prompt injection, and posts a Discord digest. The crawler described in [docs/DESIGN.md](docs/DESIGN.md) is the next build.

## Layout

| Path | What it is |
|---|---|
| `loop.sh` | Non-stop runner. One topic per iteration, then a pause. `touch STOP` pauses it. |
| `run.sh` | One research run: pick a topic, collect, write, compile the wiki, commit. |
| `tools/research.py` | Collect sources (Exa, Arctic Shift, GitHub, HN), plan, write, verify citations. |
| `tools/queue.py` | Topic queue (`topics.yml` today, SQLite next). |
| `tools/quarantine.py` | Injection scan. Scraped pages can never add topics or instructions. |
| `tools/exa_budget.py` | Exa key pool budget and live-key floor. |
| `tools/digest.py` | Daily Discord digest. The channel is set in `config.env`. |
| `prompts/research.md`, `SOUL.md` | Model instructions. |
| `systemd/` | User units: `snowball-loop.service`, `snowball-digest.{service,timer}`. |
| `docs/DESIGN.md` | Crawl decision tree, no-repeat locks and roadmap. |
| `docs/FEATURE_MATRIX.md` | Code-read comparison of 8 open-source research agents, plus what we port from each. |

## Run

```bash
cp topics.example.yml topics.yml        # seed topics
# set DIGEST_CHANNEL_ID in config.env (empty = write digests/ only)
cp systemd/snowball-*.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now snowball-loop snowball-digest.timer
```

Rules it keeps:
- It never solves captchas or bypasses bot walls. A blocked page gets marked and skipped.
- It never logs in anywhere and never fills in forms.
- It only reads public pages.
