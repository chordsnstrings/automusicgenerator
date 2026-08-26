#!/usr/bin/env bash
# Runs ON the droplet. Idempotent: safe to re-run for every deploy.
set -euo pipefail

APP=/opt/dailyfive
RELEASE="$APP/releases/$(date -u +%Y%m%dT%H%M%SZ)"

echo "==> unpacking release"
mkdir -p "$RELEASE"
tar -xzf /tmp/dailyfive.tar.gz -C "$RELEASE"
chown -R dailyfive:dailyfive "$RELEASE"

echo "==> virtualenv"
if [ ! -x "$APP/venv/bin/python" ]; then
  sudo -u dailyfive python3 -m venv "$APP/venv"
fi
sudo -u dailyfive "$APP/venv/bin/pip" install --quiet --upgrade pip wheel
sudo -u dailyfive "$APP/venv/bin/pip" install --quiet -e "$RELEASE[postgres]"

echo "==> pointing current at the new release"
ln -sfn "$RELEASE" "$APP/current"
chown -h dailyfive:dailyfive "$APP/current"

echo "==> schema migrations"
sudo -u dailyfive env $(grep -v '^#' "$APP/.env" | grep -v '^$' | xargs -d '\n') \
  "$APP/venv/bin/dailyfive" migrate

echo "==> systemd units"
cp "$RELEASE"/deploy/dailyfive-*.service "$RELEASE"/deploy/dailyfive-*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now dailyfive-web.service
systemctl enable --now dailyfive-run.timer
systemctl enable --now dailyfive-purge.timer
systemctl enable --now dailyfive-backup.timer
systemctl restart dailyfive-web.service

echo "==> pruning old releases (keeping 5)"
ls -1dt "$APP"/releases/*/ 2>/dev/null | tail -n +6 | xargs -r rm -rf

echo "==> done"
systemctl is-active dailyfive-web.service
