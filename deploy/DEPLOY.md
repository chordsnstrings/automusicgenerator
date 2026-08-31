# Deploying

Two targets exist and they are not interchangeable. `deploy.sh` installs onto a
plain droplet over SSH. Production runs on **DigitalOcean App Platform**, which
`deploy.sh` knows nothing about.

## One command

```bash
DIGITALOCEAN_ACCESS_TOKEN=dop_v1_... deploy/app-deploy.sh
```

It creates the deployment, waits for it, reads the version back out of the
running process to prove the new build is the one answering, and then runs
`verify-production.sh`. The rest of this page is what it does and why.

## App Platform: a push is not a deploy

The app's spec (`app-platform.json`) uses a **generic git source** —
`repo_clone_url` plus `branch`. App Platform only redeploys automatically for a
`github` (or `gitlab`) source with `deploy_on_push: true`, so pushing this
branch changes nothing that is running. The deployment has to be created.

```bash
export DIGITALOCEAN_ACCESS_TOKEN=...          # never committed
APP=89e7c109-b00b-4783-869b-809bbb4b06b0

curl -sS -X POST \
  -H "Authorization: Bearer $DIGITALOCEAN_ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"force_build": true}' \
  "https://api.digitalocean.com/v2/apps/$APP/deployments"
```

Then watch it, because a failed App Platform build reports as a deployment that
simply never becomes ACTIVE — there is no error at the top level:

```bash
curl -sS -H "Authorization: Bearer $DIGITALOCEAN_ACCESS_TOKEN" \
  "https://api.digitalocean.com/v2/apps/$APP/deployments?per_page=1" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin)["deployments"][0]; \
      print(d["phase"], d.get("progress",{}).get("steps") and "")'
```

## Is my change actually live?

```bash
curl -s https://dailyfive-b6bnx.ondigitalocean.app/healthz
{"ok":true,"version":"0.4.0","built":"2026-08-27T13:35:40+00:00"}
```

`built` moves with every image build. This endpoint exists because the question
was previously answered by grepping the served HTML for a CSS class that only
exists in the new version, which works exactly until it does not.

## Then verify the running system, not the tests

```bash
deploy/verify-production.sh https://dailyfive-b6bnx.ondigitalocean.app
```

Every check in that script exists because something broke in production while
the test suite was green. A green suite says the code is consistent with
itself; only the running system can say the container has the memory, the
database has the grant, and the platform has the route.

## A green deploy tells you nothing about the running system

The migration runs at container start, under an advisory lock, in both the web
service and the worker. A deployment reaching ACTIVE means the image built and
the health check passed — it does not mean the schema moved. Check `/health`
and the console before believing it.
