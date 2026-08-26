# The Daily Five

An unattended pipeline that produces **five finished songs every day** — three
full-length, two short-form — as WAV, MP3, cover art and timestamped lyrics,
into a dated folder on DigitalOcean Spaces.

Eleven agents across five phases: **sense → write → render → judge → ship**,
with a feedback loop that makes tomorrow's run different from today's.

```
7 briefs  ->  14 clips  ->  QC gate  ->  producer  ->  5 shipped
4 full                      ~11 pass                   3 full + 2 short
3 short                      ~3 cut                    6 held as reference
```

**Status: v0.3 — implemented, not yet connected.** 93 tests pass. Nothing has
run against a live API because no credentials are configured; see
[Getting started](#getting-started).

Read [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for why it is shaped this
way — the agent roster, the flow and state diagrams, the storage layout, the
Scout's free source stack, and the five settled decisions.

---

## Any brain, any role

Every reasoning role names a **role**, never a vendor. Which model answers is
configuration:

```bash
LLM_DEFAULT=minimax        # run the whole roster on MiniMax
LLM_DEFAULT=anthropic      # ...or on Claude
LLM_PRODUCER=anthropic     # ...and keep one role on a stronger brain
```

Providers: `anthropic`, `minimax`, and `openai-compatible` for anything else
speaking that dialect — OpenRouter, Together, vLLM, Ollama — via `LLM_BASE_URL`.
A spec is `provider` or `provider:model`.

```bash
dailyfive brains            # which brain runs which role
dailyfive brains --probe    # ...and whether each one answers
```

The differences that matter between brains are handled in one place
(`src/dailyfive/llm.py`), not scattered through the agents: native JSON mode
where the provider has one, reasoning-token suppression, and one error
vocabulary. Verified end to end on MiniMax M3 — the full roster against live
trend feeds, 11 calls, zero failures.

## Seeing what it did

The day page shows five songs. The **console** shows the machine that made them:

| Page | Shows |
|---|---|
| `/` | runs, learning signal, which brain backs which role |
| `/runs/<date>` | the themes the Scout found and from which feed, every brief, every job, **all 14 candidates including the nine that did not ship and exactly why**, every brain call with timing |
| `/agents` | all eleven roles, their brains, activity and failures |
| `/codex` | the Style Codex, persona cast, what it has learned, version history |
| `/files` | everything delivered, by day |

Run it with `dailyfive serve`. It is the same service that receives Suno's
callbacks, so there is still one public surface to secure.

## The two things worth knowing up front

**Four of the eleven agents have no language model in them at all.** The
Conductor, the QC Engineer, and the daily halves of the Packager and Archivist
are plain code. That is what makes this cheap enough to run 365 days a year and
reliable enough to run unattended. Audio quality is decided by *measurement*,
never judgment — put a model where a measurement belongs and you get a system
that hallucinates that the audio is fine.

**The WAV is deliberately not loudness-normalised.** DSPs apply their own
normalisation, and delivering pre-normalised discards headroom you cannot
recover. `master.wav` is a true delivery master, trimmed and faded only;
`master.mp3` carries the −14 LUFS pass and is the one you will actually play.

---

## Getting started

### 1. Install

```bash
git clone <this repo> && cd automusicgenerator
pip install -e ".[dev]"          # add [postgres] on the droplet
cp .env.example .env             # then fill it in — see below
dailyfive init-db
```

`ffmpeg` must be on PATH. QC measurement and MP3 encoding both depend on it:
`apt-get install -y ffmpeg`.

### 2. Check the free feeds — costs nothing

```bash
dailyfive signals
```

Three of the seven need no credentials at all (Google Trends RSS, Deezer,
Apple Music RSS), so this works before you have configured anything.

### 3. Preflight — costs nothing

```bash
dailyfive preflight
```

Reports every missing key, unreachable service and setup step **at once**. One
error listing four problems beats four consecutive runs each dying on the next.

### 4. Create the persona cast — costs one generation each

```bash
dailyfive personas bootstrap
```

Generates a seed song per persona and builds a Suno persona from a vocal
segment of it. Without this, songs render with a generic voice and nothing
accumulates across releases.

### 5. Run

```bash
dailyfive serve          # the callback + rating receiver (needs a public HTTPS URL)
dailyfive run            # the daily pipeline
dailyfive today          # what shipped, with clip ids
dailyfive rate 42 8      # close the loop
```

---

## The loop only closes if you rate

Each day's delivered `index.html` carries a 1–10 control per song that posts
back to the same endpoint receiving Suno's callbacks. Thirty seconds a morning.

`dailyfive status` tells you which signal the system is actually optimising
against, in plain words:

```
learning signal: producer-only — no ratings recorded yet, so the loop is
optimising for the Producer agent's opinion
```

That message changes to `rating-led` once you have rated more than half of what
shipped. Until then the system is grading its own homework, and it says so.

---

## Commands

| Command | Does | Costs |
|---|---|---|
| `dailyfive preflight` | check everything before spending a credit | free |
| `dailyfive signals` | test the seven trend feeds | free |
| `dailyfive brains [--probe]` | which brain runs which role | free / a few tokens |
| `dailyfive credits` | Suno balance | free |
| `dailyfive personas list \| bootstrap \| set` | manage the recurring cast | one generation each |
| `dailyfive run [--date] [--skip-art]` | the daily pipeline | the day's credits |
| `dailyfive today [--date]` | what shipped, with clip ids | free |
| `dailyfive rate <clip_id> <1-10>` | record your rating | free |
| `dailyfive status` | recent runs and the learning signal | free |
| `dailyfive retro [--dry-run]` | weekly codex retrospective | one model call |
| `dailyfive serve` | callback and rating receiver | free |

---

## Stack

| Layer | Choice |
|---|---|
| Music | [sunoapi.org](https://docs.sunoapi.org/) — the only irreplaceable dependency |
| Reasoning roles | any brain — MiniMax, Claude, or an OpenAI-compatible endpoint |
| Cover art | BytePlus ModelArk — Seedream, 3000×3000 (no text models there) |
| Audio QC + mastering | ffmpeg only — `ebur128`, `astats`, `silencedetect`. No LLM. |
| Storage | DigitalOcean Spaces (S3-compatible) |
| State | Postgres (SQLite works for day one) |
| Host | one small droplet + systemd timer + webhook endpoint |

## Deploying

Two shapes, and the choice comes down to one requirement: **the callback
endpoint needs real HTTPS**, because Suno will not post to a self-signed
certificate and the browser will not let the day page rate a song over
plain HTTP.

### DigitalOcean App Platform — the default

`deploy/app-platform.json` is the app spec: one web service, one worker on
the same image, and a linked managed Postgres. TLS on the
`*.ondigitalocean.app` hostname is issued and renewed by the platform at no
charge, so there is no certbot, no renewal timer, and no cron job that
silently stops renewing.

```bash
doctl apps create --spec deploy/app-platform.json
psql "$ADMIN_DATABASE_URL" -f deploy/grant-schema.sql   # once, see below
```

Two things bite on a fresh cluster, and both are invisible from the error:

* **The database firewall.** A tag rule does not cover an App Platform app —
  apps are not droplets and carry no tags. Add an explicit
  `{"type": "app", "value": "<app-id>"}` trusted source, or every connection
  is refused with `server closed the connection unexpectedly`.
* **Schema ownership.** Postgres 15 removed the implicit `CREATE` on
  `public`, and a managed database belongs to the provider's admin role — so
  the application user connects fine and cannot create a single table.
  `deploy/grant-schema.sql` fixes it once, as the admin role.

The worker keeps its own clock (`dailyfive scheduler`): purge 03:00, backup
04:30, run 05:10, all UTC.

### A droplet — when you want the box

```bash
sudo APP_DIR=/opt/dailyfive bash deploy/setup.sh
```

Installs ffmpeg and Postgres, creates the service user, and enables a systemd
timer at 05:10 UTC plus the receiver. `deploy/nginx.conf.example` has the
reverse-proxy shape, and certbot is yours to run and to keep renewing.

## Tests

```bash
pytest                    # 132 tests
pytest -m "not network"   # skip the four that hit live feeds
```

The suite runs with no credentials and no ffmpeg. `tests/test_end_to_end.py`
runs a complete day against fakes — 7 briefs, 14 clips, QC, selection,
packaging, delivery, and a simulated mid-run crash that must resume without
re-spending.
