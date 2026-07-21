#!/usr/bin/env bash
# One-command redeploy FROM your dev machine TO the live server.
#
#   bash deploy/push.sh                 # git push (if needed) + run update.sh on the server
#   bash deploy/push.sh --frontend      # also force a dashboard rebuild
#   bash deploy/push.sh --no-git        # skip the local git push; just redeploy the server
#
# It reads the concrete server address from deploy/target.local.env (gitignored;
# see target.local.env.example). This is the fix for "a fresh session doesn't
# know the server address" — the address lives in a file, not in someone's head.
#
# What it does, in order:
#   1. push the current branch to origin (so the server's `git pull` sees it),
#   2. SSH in and run deploy/update.sh (pull + deps + build-if-changed + restart),
#   3. print the health-check result.
#
# NOTE: update.sh ends with `sudo systemctl restart tradebot` — ~15s downtime.
# Avoid running it inside market hours (09:15–15:30 IST) while a position is open.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$SCRIPT_DIR/target.local.env"

if [ ! -f "$ENV_FILE" ]; then
    echo "ERROR: $ENV_FILE not found." >&2
    echo "  Copy deploy/target.local.env.example to deploy/target.local.env and fill it in." >&2
    exit 1
fi
# shellcheck disable=SC1090
source "$ENV_FILE"

DO_GIT=1
FRONTEND=""
for arg in "$@"; do
    case "$arg" in
        --no-git)   DO_GIT=0 ;;
        --frontend) FRONTEND="--frontend" ;;
        *) echo "unknown arg: $arg" >&2; exit 2 ;;
    esac
done

SSH_KEY="${TRADEBOT_SSH_KEY/#\~/$HOME}"      # expand a leading ~
HOST="${TRADEBOT_SERVER_IP:-$TRADEBOT_DOMAIN}"
USER="${TRADEBOT_SSH_USER:-ubuntu}"
REMOTE_DIR="${TRADEBOT_REMOTE_DIR:-~/tradebot}"

if [ ! -f "$SSH_KEY" ]; then
    echo "ERROR: SSH key not found at $SSH_KEY (from TRADEBOT_SSH_KEY)." >&2
    exit 1
fi

if [ "$DO_GIT" = "1" ]; then
    BRANCH="$(git -C "$PROJECT_ROOT" rev-parse --abbrev-ref HEAD)"
    echo "==> Pushing $BRANCH to origin"
    git -C "$PROJECT_ROOT" push origin "$BRANCH"
fi

echo "==> Deploying to ${USER}@${HOST}:${REMOTE_DIR}"
ssh -i "$SSH_KEY" -o StrictHostKeyChecking=accept-new "${USER}@${HOST}" \
    "cd ${REMOTE_DIR} && bash deploy/update.sh ${FRONTEND}"

echo "==> Done. Dashboard: https://${TRADEBOT_DOMAIN:-$HOST}"
