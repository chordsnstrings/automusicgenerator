#!/usr/bin/env bash
# Runs locally. Ships the working tree to the droplet and installs it.
#
#   DROPLET_IP=1.2.3.4 SSH_KEY=~/.ssh/dailyfive_deploy ./deploy/deploy.sh
#
# The .env is never shipped — it lives on the droplet and holds the secrets.
set -euo pipefail

: "${DROPLET_IP:?set DROPLET_IP}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/dailyfive_deploy}"
SSH_OPTS="-o StrictHostKeyChecking=accept-new -i $SSH_KEY"

echo "==> building release tarball"
tar --exclude-vcs \
    --exclude='./work' --exclude='./.env' --exclude='./.venv' \
    --exclude='__pycache__' --exclude='.pytest_cache' --exclude='*.egg-info' \
    -czf /tmp/dailyfive.tar.gz \
    src migrations tests deploy pyproject.toml alembic.ini README.md docs

echo "==> shipping to $DROPLET_IP"
scp $SSH_OPTS /tmp/dailyfive.tar.gz "root@$DROPLET_IP:/tmp/dailyfive.tar.gz"
scp $SSH_OPTS deploy/install.sh "root@$DROPLET_IP:/tmp/install.sh"

echo "==> installing"
ssh $SSH_OPTS "root@$DROPLET_IP" "bash /tmp/install.sh"

echo "==> health"
ssh $SSH_OPTS "root@$DROPLET_IP" "curl -sf localhost:8080/health || true"
echo
