#!/usr/bin/env bash
# Provision a fresh Ubuntu droplet for The Daily Five.
# Idempotent — safe to re-run after editing .env.
set -euo pipefail

APP_USER="${APP_USER:-dailyfive}"
APP_DIR="${APP_DIR:-/opt/dailyfive}"

echo "==> packages"
apt-get update -qq
# ffmpeg is not optional: QC measurement and MP3 encoding both depend on it.
apt-get install -y -qq python3.11 python3.11-venv python3-pip ffmpeg \
                      postgresql postgresql-contrib nginx certbot \
                      python3-certbot-nginx git

echo "==> user and directories"
id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --create-home --shell /bin/bash "$APP_USER"
mkdir -p "$APP_DIR" "$APP_DIR/work"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

echo "==> database"
sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='dailyfive'" | grep -q 1 || \
  sudo -u postgres psql -c "CREATE USER dailyfive WITH PASSWORD '${DB_PASSWORD:-changeme}';"
sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='dailyfive'" | grep -q 1 || \
  sudo -u postgres createdb -O dailyfive dailyfive

echo "==> virtualenv"
sudo -u "$APP_USER" python3.11 -m venv "$APP_DIR/venv"
sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip" install --quiet -e "$APP_DIR[postgres]"

if [ ! -f "$APP_DIR/.env" ]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  chown "$APP_USER:$APP_USER" "$APP_DIR/.env"
  chmod 600 "$APP_DIR/.env"
  echo "!! Edit $APP_DIR/.env with your API keys before continuing."
fi

echo "==> services"
cp "$APP_DIR/deploy/dailyfive-web.service" /etc/systemd/system/
cp "$APP_DIR/deploy/dailyfive-run.service" /etc/systemd/system/
cp "$APP_DIR/deploy/dailyfive-run.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now dailyfive-web.service
systemctl enable --now dailyfive-run.timer

echo "==> database schema"
sudo -u "$APP_USER" "$APP_DIR/venv/bin/dailyfive" init-db

cat <<'NEXT'

Done. Remaining steps, in order:

  1. Edit /opt/dailyfive/.env with your API keys.
  2. Point a DNS A record at this droplet, then:
       certbot --nginx -d songs.yourdomain.com
     Suno must be able to reach the callback URL over HTTPS or no run completes.
  3. sudo -u dailyfive /opt/dailyfive/venv/bin/dailyfive preflight
  4. sudo -u dailyfive /opt/dailyfive/venv/bin/dailyfive personas bootstrap
     (costs one generation per persona — creates the recurring cast)
  5. sudo -u dailyfive /opt/dailyfive/venv/bin/dailyfive run

  Logs:   journalctl -u dailyfive-web -f
          journalctl -u dailyfive-run -f
  Timer:  systemctl list-timers dailyfive-run

NEXT
