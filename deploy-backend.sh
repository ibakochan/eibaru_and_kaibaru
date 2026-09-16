#!/usr/bin/env bash
set -euo pipefail

REMOTE="bitnami@13.158.230.76"
REMOTE_DIR="/home/bitnami/dp/mysite"
SSH_KEY="$HOME/.ssh/ibacho.pem"

echo "======================================"
echo " Kaibaru Backend Deployment"
echo "======================================"

# --------------------------------------------------
# 1. Make sure local working tree is clean
# --------------------------------------------------

echo "--- Checking local Git status ---"

if [[ -n "$(git status --porcelain)" ]]; then
    echo ""
    echo "ERROR: You have uncommitted changes."
    echo "Commit them before deploying."
    echo ""
    git status
    exit 1
fi

# --------------------------------------------------
# 2. Make sure local branch is pushed to GitHub
# --------------------------------------------------

echo "--- Checking GitHub ---"

git fetch origin

LOCAL_COMMIT=$(git rev-parse HEAD)
REMOTE_COMMIT=$(git rev-parse origin/main)

if [[ "$LOCAL_COMMIT" != "$REMOTE_COMMIT" ]]; then
    echo ""
    echo "ERROR: Your local main branch is not the same as origin/main."
    echo ""
    echo "Run:"
    echo "  git push origin main"
    echo ""
    exit 1
fi

echo "Local code matches origin/main."

# --------------------------------------------------
# 3. Deploy to Lightsail
# --------------------------------------------------

echo "--- Deploying to Lightsail ---"

ssh -i "$SSH_KEY" "$REMOTE" "
    set -e
    cd '$REMOTE_DIR'

    echo '--- Pulling latest code ---'
    git pull --ff-only origin main

    echo '--- Running Django migrations ---'
    ./venv/bin/python manage.py migrate --noinput

    echo '--- Running collectstatic ---'
    ./venv/bin/python manage.py collectstatic --noinput

    echo '--- Restarting Daphne ---'
    sudo systemctl restart kaibaru_daphne

    echo '--- Restarting Celery worker ---'
    sudo systemctl restart celery-worker

    echo '--- Restarting Celery Beat ---'
    sudo systemctl restart celery-beat

    echo '--- Checking services ---'
    systemctl is-active --quiet kaibaru_daphne
    systemctl is-active --quiet celery-worker
    systemctl is-active --quiet celery-beat

    echo 'All services are running.'
"

echo ""
echo "======================================"
echo " DEPLOYMENT SUCCESSFUL"
echo "======================================"
