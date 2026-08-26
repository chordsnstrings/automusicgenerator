# automusicgenerator

An unattended pipeline that produces **five finished songs every day** — WAV, MP3,
cover art and timestamped lyrics — into a dated folder on DigitalOcean Spaces.

Eleven agents across five phases: **sense → write → render → judge → ship**, with a
feedback loop that makes tomorrow's run different from today's.

```
6 briefs  ->  12 clips  ->  QC gate  ->  producer  ->  5 shipped
                            ~9 pass                    4 held as reference
                            ~3 cut                     (rejection reasons logged)
```

**Status: proposal, v0.1. No code yet.**

Read [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) first — it carries the agent
roster, the flow and state diagrams, the storage layout, provider assignment, cost
estimates, and the five open decisions that need answering before implementation.

## Stack

| Layer | Choice |
|---|---|
| Music | [sunoapi.org](https://docs.sunoapi.org/) |
| Lyrics | MiniMax M3 |
| Cover art | BytePlus ModelArk (Seedream) |
| Reasoning agents | Claude |
| Audio QC + mastering | ffmpeg / librosa — no LLM |
| Storage | DigitalOcean Spaces (S3-compatible) |
| State | Postgres |
| Host | one small DigitalOcean droplet + cron + webhook endpoint |
