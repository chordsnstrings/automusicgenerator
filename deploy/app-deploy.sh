#!/usr/bin/env bash
# Deploy to App Platform and prove the new build is the one answering.
#
#   DIGITALOCEAN_ACCESS_TOKEN=dop_v1_... deploy/app-deploy.sh
#
# This exists because a push is not a deploy. The app's spec uses a generic git
# source (repo_clone_url + branch), and App Platform only redeploys
# automatically for a github source with deploy_on_push — so pushing this branch
# changes nothing that is running, and the studio has more than once been three
# commits behind its own repository without anything saying so.
#
# It does not stop at "deployment ACTIVE". A deployment reaching ACTIVE means
# the image built and the health check passed; it does not mean the schema
# moved or that the code you pushed is what answers. So the last thing this does
# is read the version back out of the running process.
set -uo pipefail

APP="${APP_ID:-89e7c109-b00b-4783-869b-809bbb4b06b0}"
BASE="${PUBLIC_BASE_URL:-https://dailyfive-b6bnx.ondigitalocean.app}"
: "${DIGITALOCEAN_ACCESS_TOKEN:?set DIGITALOCEAN_ACCESS_TOKEN}"
API="https://api.digitalocean.com/v2/apps/$APP"
AUTH=(-H "Authorization: Bearer $DIGITALOCEAN_ACCESS_TOKEN")

before=$(curl -sS --max-time 20 "$BASE/healthz" || echo '{}')
echo "running now : $before"

echo "==> creating deployment"
dep=$(curl -sS -X POST "${AUTH[@]}" -H "Content-Type: application/json" \
        -d '{"force_build": true}' "$API/deployments" \
      | python3 -c 'import json,sys
d=json.load(sys.stdin)
if "deployment" not in d:
    print("ERROR", d.get("message") or d, file=sys.stderr); raise SystemExit(1)
print(d["deployment"]["id"])') || exit 1
echo "    deployment $dep"

echo "==> waiting (a build is about 4-6 minutes)"
for _ in $(seq 1 60); do
  sleep 20
  phase=$(curl -sS --max-time 30 "${AUTH[@]}" "$API/deployments/$dep" \
          | python3 -c 'import json,sys; print(json.load(sys.stdin)["deployment"]["phase"])' \
          2>/dev/null || echo UNKNOWN)
  printf '    %s  %s\n' "$(date -u +%H:%M:%S)" "$phase"
  case "$phase" in
    ACTIVE) break ;;
    ERROR|CANCELED|FAILED)
      echo "deployment $phase — the build log is at:"
      echo "  https://cloud.digitalocean.com/apps/$APP/deployments/$dep"
      exit 1 ;;
  esac
done
[ "${phase:-}" = "ACTIVE" ] || { echo "gave up waiting; still $phase"; exit 1; }

# A green deploy tells you nothing about the running system. The version is
# what does: it changes with every image build and with nothing else.
echo "==> what is answering now"
for _ in $(seq 1 15); do
  after=$(curl -sS --max-time 20 "$BASE/healthz" || echo '{}')
  case "$after" in *'"version"'*) break ;; esac
  sleep 10
done
echo "    $after"
if [ "$after" = "$before" ]; then
  echo "FAIL — /healthz is unchanged. The deployment went ACTIVE but the old"
  echo "       build is still serving. Check the component logs before believing"
  echo "       anything else on the console."
  exit 1
fi

echo "==> verifying the running system"
exec "$(dirname "$0")/verify-production.sh" "$BASE"
