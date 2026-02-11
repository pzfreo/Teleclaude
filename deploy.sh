#!/usr/bin/env bash
set -euo pipefail

# deploy.sh — Deploy Teleclaude to a DigitalOcean droplet
#
# Usage:
#   First time:  ./deploy.sh setup <droplet-ip>
#   Updates:     ./deploy.sh <droplet-ip>
#
# Prerequisites:
#   - SSH access to the droplet (key-based)
#   - .env file in this directory

HOST="${2:-${1:-}}"
COMMAND="${1:-deploy}"
REMOTE_DIR="/opt/teleclaude"
SSH_USER="root"

if [ -z "$HOST" ]; then
    echo "Usage:"
    echo "  ./deploy.sh setup <droplet-ip>   # First-time setup"
    echo "  ./deploy.sh <droplet-ip>          # Deploy update"
    exit 1
fi

# If first arg doesn't look like a command, treat it as host (deploy shortcut)
if [[ "$COMMAND" != "setup" ]]; then
    HOST="$COMMAND"
    COMMAND="deploy"
fi

ssh_run() {
    ssh -o StrictHostKeyChecking=accept-new "${SSH_USER}@${HOST}" "$@"
}

case "$COMMAND" in
    setup)
        echo "==> Setting up $HOST..."

        # Install Docker
        ssh_run 'command -v docker >/dev/null 2>&1 || {
            curl -fsSL https://get.docker.com | sh
            systemctl enable docker
        }'

        # Create app directory
        ssh_run "mkdir -p ${REMOTE_DIR}"

        # Copy project files
        echo "==> Copying project files..."
        scp -o StrictHostKeyChecking=accept-new \
            Dockerfile docker-compose.yml requirements.txt VERSION *.py \
            "${SSH_USER}@${HOST}:${REMOTE_DIR}/"

        # Copy .env if it exists
        if [ -f .env ]; then
            echo "==> Copying .env..."
            scp "${SSH_USER}@${HOST}:${REMOTE_DIR}/.env" 2>/dev/null && true
            scp .env "${SSH_USER}@${HOST}:${REMOTE_DIR}/.env"
        else
            echo "WARNING: No .env file found. Create one on the server at ${REMOTE_DIR}/.env"
        fi

        # Build and start
        echo "==> Building and starting..."
        ssh_run "cd ${REMOTE_DIR} && docker compose up -d --build"

        echo "==> Done! Teleclaude is running on $HOST"
        echo "    Logs: ssh ${SSH_USER}@${HOST} 'cd ${REMOTE_DIR} && docker compose logs -f'"
        ;;

    deploy)
        echo "==> Deploying to $HOST..."

        # Copy updated files
        scp -o StrictHostKeyChecking=accept-new \
            Dockerfile docker-compose.yml requirements.txt VERSION *.py \
            "${SSH_USER}@${HOST}:${REMOTE_DIR}/"

        # Rebuild and restart
        ssh_run "cd ${REMOTE_DIR} && docker compose up -d --build"

        echo "==> Deployed. Checking status..."
        ssh_run "cd ${REMOTE_DIR} && docker compose ps"
        ;;
esac
