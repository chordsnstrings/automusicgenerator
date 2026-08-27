#!/usr/bin/env bash
# Prove a deployment works by asking the deployment, not by asking the tests.
#
#   deploy/verify-production.sh https://dailyfive-b6bnx.ondigitalocean.app
#
# Every check here exists because something actually broke in production and the
# test suite was green at the time. A green suite says the code is consistent
# with itself; only the running system can say the container has the memory, the
# database has the grant, and the platform has the route.
#
# Exits non-zero on the first failure, so it can gate a deploy.
set -uo pipefail

BASE="${1:-${PUBLIC_BASE_URL:-}}"
[ -n "$BASE" ] || { echo "usage: $0 https://host"; exit 2; }
BASE="${BASE%/}"
FAILED=0

say()  { printf '%-46s %s\n' "$1" "$2"; }
ok()   { say "$1" "ok — $2"; }
bad()  { say "$1" "FAIL — $2"; FAILED=$((FAILED + 1)); }

code() { curl -sS -o /dev/null -w '%{http_code}' --max-time "${3:-45}" ${2:+-H "$2"} "$BASE$1"; }

# ── which build answered ─────────────────────────────────────────────────────
# First, because every check below is a statement about a version, and this app
# deploys from a generic git source — App Platform does not redeploy one of
# those on a push, so "the tests pass and I pushed" is not evidence that any of
# this is running.
build=$(curl -sS --max-time 20 "$BASE/healthz")
case "$build" in
  *'"version"'*) ok "build" "$build" ;;
  *) bad "build" "no version in /healthz: ${build:-no answer}" ;;
esac

# ── the pages ────────────────────────────────────────────────────────────────
# /genres is the heaviest page here — it calls scores(), status(), trend() and
# slate(), and indexes into Run.notes JSON written by sixty runs of possibly
# different shapes. A 500 on it is exactly the kind of failure a green test
# suite cannot see, which is what this script is for.
for p in / /runs /agents /codex /genres /files /storage /health /healthz; do
  c=$(code "$p")
  [ "$c" = "200" ] && ok "GET $p" "200" || bad "GET $p" "$c"
done

# ── HTTPS is the whole reason this runs on App Platform: Suno will not post a
#    callback to anything less, so a broken redirect is a broken pipeline.
c=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 30 "http://${BASE#https://}/healthz")
[ "$c" = "301" ] && ok "http:// redirects" "301" || bad "http:// redirects" "$c"

# ── the console must be somewhere you can listen ─────────────────────────────
# The Files page listing filenames as text, with no way to play anything, is the
# defect this check exists to catch. Counting <audio> occurrences rather than
# lines: the markup is assembled with joins, so many tags share a line.
players=$(curl -sS --max-time 45 "$BASE/files" | grep -o 'src="/files/[^"]*master\.mp3"' | sort -u | wc -l)
[ "$players" -gt 0 ] && ok "players on /files" "$players distinct songs" \
                     || bad "players on /files" "no audio sources"

# ── streaming ────────────────────────────────────────────────────────────────
# Pick the largest thing on offer. A WAV is ~57 MB, and reading one whole column
# into a 512 MB container is what returned 504 and restarted the box in
# production on 2026-08-26. If this check passes, that cannot have come back.
KEY=$(curl -sS --max-time 45 "$BASE/files" | grep -o '/files/[^"]*master\.wav' | head -1)
if [ -z "$KEY" ]; then
  say "stream a master.wav" "skipped — nothing delivered yet"
else
  read -r c bytes ttfb <<<"$(curl -sS -o /dev/null -w '%{http_code} %{size_download} %{time_starttransfer}' --max-time 300 "$BASE$KEY")"
  if [ "$c" = "200" ] && [ "$bytes" -gt 1000000 ]; then
    ok "stream a master.wav" "200, ${bytes}B, first byte ${ttfb}s"
  else
    bad "stream a master.wav" "$c after ${bytes}B — the OOM is back"
  fi
  # Still serving afterwards is the actual assertion: the failure mode was the
  # container dying, which a single response code cannot show.
  sleep 2
  c=$(code /healthz)
  [ "$c" = "200" ] && ok "still up after the WAV" "200" \
                   || bad "still up after the WAV" "$c — container went away"

  # A dead scrub bar is a broken player, and only a 206 makes one work.
  read -r c bytes <<<"$(curl -sS -o /dev/null -w '%{http_code} %{size_download}' -H 'Range: bytes=0-99999' --max-time 60 "$BASE$KEY")"
  [ "$c" = "206" ] && [ "$bytes" = "100000" ] && ok "Range request" "206, ${bytes}B" \
                                              || bad "Range request" "$c/${bytes}B"
  c=$(code "$KEY" "Range: bytes=99999999999-")
  [ "$c" = "416" ] && ok "unsatisfiable Range" "416" || bad "unsatisfiable Range" "$c"
fi

# ── the shape of the day ─────────────────────────────────────────────────────
# /health is the only endpoint that answers "did the studio actually work",
# which is the failure this system is most likely to have: not a crash, a day
# that quietly did not happen.
# Nested quotes inside an f-string expression need Python 3.12; this script has
# to run wherever an operator happens to be standing, so it does not use them.
curl -sS --max-time 45 "$BASE/health" | python3 -c '
import json, sys
h = json.load(sys.stdin)
label = "health checks"
state = "ok" if h.get("ok") else "FAIL"
db = h.get("checks", {}).get("database")
print("%-46s %s — db=%s, latest=%s, phase=%s"
      % (label, state, db, h.get("latest_run"), h.get("phase")))
sys.exit(0 if h.get("ok") else 1)' || FAILED=$((FAILED + 1))

# Storage is the one figure here that can end the studio rather than interrupt
# it: a managed Postgres that fills its disk goes read-only, and with
# AUDIO_STORE=database the only copy of the delivered audio is inside it. So
# this asserts rather than prints — but only where the disk has been declared,
# because guessing a cluster's capacity would be a worse failure than not
# knowing it.
curl -sS --max-time 45 "$BASE/storage" | python3 -c '
import json, sys
s = json.load(sys.stdin)
print("%-46s ok — %s files, %s GB, %sd retention, %s due, settles at %s GB"
      % ("storage", s["files"], s["total_gb"], s["retention_days"],
         s["due_now"], s.get("projected_steady_gb")))
head = s.get("headroom_gb")
if s.get("disk_gb") is None:
    print("%-46s ok — DB_DISK_GB is not set, so headroom is unknown"
          % "storage headroom")
elif head is None or head > 0:
    print("%-46s ok — %s GB spare on a %s GB disk" % ("storage headroom", head, s["disk_gb"]))
else:
    print("%-46s FAIL — settles at %s GB on a %s GB disk"
          % ("storage headroom", s.get("projected_steady_gb"), s["disk_gb"]))
    sys.exit(1)' || FAILED=$((FAILED + 1))

# ── things that must NOT work ────────────────────────────────────────────────
c=$(code "/files/../../etc/passwd"); [ "$c" = "404" ] && ok "path traversal" "404" || bad "path traversal" "$c"
c=$(code "/files/songs/nope/nope.mp3"); [ "$c" = "404" ] && ok "unknown key" "404" || bad "unknown key" "$c"
c=$(curl -sS -o /dev/null -w '%{http_code}' -X POST -H 'content-type: application/json' \
      -d '{}' --max-time 30 "$BASE/webhooks/wrong/generate")
# A 403 would confirm the path exists. A 404 tells a scanner nothing.
[ "$c" = "404" ] && ok "wrong webhook secret" "404" || bad "wrong webhook secret" "$c"

echo
[ "$FAILED" -eq 0 ] && echo "production verified" || echo "$FAILED check(s) failed"
exit $([ "$FAILED" -eq 0 ] && echo 0 || echo 1)
