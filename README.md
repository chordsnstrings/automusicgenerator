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
| Lyrics | MiniMax M3 (ModelArk exposes no text models) |
| Cover art | BytePlus ModelArk — Seedream, 3000×3000 |
| Reasoning agents | Claude — Scout, Director, A&R, Clearance, Producer |
| Audio QC + mastering | ffmpeg only — `ebur128`, `astats`, `silencedetect`. No LLM. |
| Storage | DigitalOcean Spaces (S3-compatible) |
| State | Postgres (SQLite works for day one) |
| Host | one small droplet + systemd timer + webhook endpoint |

## Deploying

```bash
sudo APP_DIR=/opt/dailyfive bash deploy/setup.sh
```

Installs ffmpeg and Postgres, creates the service user, and enables a systemd
timer at 05:10 UTC plus the receiver. `deploy/nginx.conf.example` has the
reverse-proxy shape; the callback endpoint needs real HTTPS, because Suno will
not post to a self-signed certificate.

## Tests

```bash
pytest                    # 93 tests
pytest -m "not network"   # skip the four that hit live feeds
```

The suite runs with no credentials and no ffmpeg. `tests/test_end_to_end.py`
runs a complete day against fakes — 7 briefs, 14 clips, QC, selection,
packaging, delivery, and a simulated mid-run crash that must resume without
re-spending.
