#!/usr/bin/env bash
set -euo pipefail

# deploy.sh — Create, deploy, and manage a Teleclaude droplet on DigitalOcean
#
# Usage:
#   ./deploy.sh create              # Create droplet + deploy
#   ./deploy.sh setup <droplet-ip>  # First-time setup on existing server
#   ./deploy.sh [<droplet-ip>]      # Deploy update (uses saved IP if omitted)
#   ./deploy.sh logs                # Tail logs
#   ./deploy.sh ssh                 # SSH into the droplet
#   ./deploy.sh destroy             # Tear down the droplet
#
# Prerequisites:
#   - doctl authenticated (for create/destroy)
#   - SSH key on DigitalOcean
#   - .env file in this directory

DROPLET_NAME="teleclaude"
DROPLET_SIZE="s-1vcpu-2gb"
DROPLET_IMAGE="ubuntu-24-04-x64"
DROPLET_REGION="sfo3"
REMOTE_DIR="/opt/teleclaude"
SSH_USER="root"
IP_FILE=".droplet-ip"

# ── Helpers ──────────────────────────────────────────────────────────

get_ip() {
    if [ -f "$IP_FILE" ]; then
        cat "$IP_FILE"
    else
        echo ""
    fi
}

save_ip() {
    echo "$1" > "$IP_FILE"
    echo "Droplet IP saved to $IP_FILE"
}

ssh_run() {
    local host="$1"; shift
    ssh -o StrictHostKeyChecking=accept-new "${SSH_USER}@${host}" "$@"
}

wait_for_ssh() {
    local host="$1"
    echo "==> Waiting for SSH on $host..."
    for i in $(seq 1 30); do
        if ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=5 "${SSH_USER}@${host}" true 2>/dev/null; then
            echo "    SSH ready."
            return 0
        fi
        sleep 5
    done
    echo "ERROR: SSH not available after 150s"
    exit 1
}

copy_files() {
    local host="$1"
    scp -o StrictHostKeyChecking=accept-new \
        Dockerfile docker-compose.yml requirements.txt VERSION *.py \
        "${SSH_USER}@${host}:${REMOTE_DIR}/"
}

# ── Commands ─────────────────────────────────────────────────────────

COMMAND="${1:-deploy}"
shift || true

case "$COMMAND" in
    create)
        if [ ! -f .env ]; then
            echo "ERROR: .env file not found. Copy .env.example and fill in your values."
            exit 1
        fi

        echo "==> Creating droplet: $DROPLET_NAME ($DROPLET_SIZE in $DROPLET_REGION)..."

        # Use first available SSH key
        SSH_KEY_ID=$(doctl compute ssh-key list --format ID --no-header | head -1)
        if [ -z "$SSH_KEY_ID" ]; then
            echo "ERROR: No SSH keys found on your DO account. Add one first."
            exit 1
        fi

        # Create droplet
        IP=$(doctl compute droplet create "$DROPLET_NAME" \
            --size "$DROPLET_SIZE" \
            --image "$DROPLET_IMAGE" \
            --region "$DROPLET_REGION" \
            --ssh-keys "$SSH_KEY_ID" \
            --wait \
            --format PublicIPv4 \
            --no-header)

        save_ip "$IP"
        echo "==> Droplet created: $IP"

        wait_for_ssh "$IP"

        # Install Docker
        echo "==> Installing Docker..."
        ssh_run "$IP" 'curl -fsSL https://get.docker.com | sh && systemctl enable docker'

        # Create app directory and copy files
        ssh_run "$IP" "mkdir -p ${REMOTE_DIR}"
        echo "==> Copying project files..."
        copy_files "$IP"

        echo "==> Copying .env..."
        scp -o StrictHostKeyChecking=accept-new .env "${SSH_USER}@${IP}:${REMOTE_DIR}/.env"
        ssh_run "$IP" "chmod 600 ${REMOTE_DIR}/.env"

        # Build and start
        echo "==> Building and starting..."
        ssh_run "$IP" "cd ${REMOTE_DIR} && docker compose up -d --build"

        echo ""
        echo "==> Teleclaude is running on $IP"
        echo "    Logs:    ./deploy.sh logs"
        echo "    SSH:     ./deploy.sh ssh"
        echo "    Update:  ./deploy.sh"
        echo "    Destroy: ./deploy.sh destroy"
        ;;

    setup)
        HOST="${1:-}"
        if [ -z "$HOST" ]; then
            echo "Usage: ./deploy.sh setup <droplet-ip>"
            exit 1
        fi
        save_ip "$HOST"

        echo "==> Setting up $HOST..."

        # Install Docker
        ssh_run "$HOST" 'command -v docker >/dev/null 2>&1 || {
            curl -fsSL https://get.docker.com | sh
            systemctl enable docker
        }'

        ssh_run "$HOST" "mkdir -p ${REMOTE_DIR}"

        echo "==> Copying project files..."
        copy_files "$HOST"

        if [ -f .env ]; then
            echo "==> Copying .env..."
            scp -o StrictHostKeyChecking=accept-new .env "${SSH_USER}@${HOST}:${REMOTE_DIR}/.env"
            ssh_run "$HOST" "chmod 600 ${REMOTE_DIR}/.env"
        else
            echo "WARNING: No .env file found. Create one on the server at ${REMOTE_DIR}/.env"
        fi

        echo "==> Building and starting..."
        ssh_run "$HOST" "cd ${REMOTE_DIR} && docker compose up -d --build"

        echo "==> Done! Teleclaude is running on $HOST"
        ;;

    deploy)
        HOST="${1:-$(get_ip)}"
        if [ -z "$HOST" ]; then
            echo "Usage: ./deploy.sh [<droplet-ip>]"
            echo "Or run ./deploy.sh create first."
            exit 1
        fi

        echo "==> Deploying to $HOST..."
        copy_files "$HOST"

        ssh_run "$HOST" "cd ${REMOTE_DIR} && docker compose up -d --build"

        echo "==> Deployed. Checking status..."
        ssh_run "$HOST" "cd ${REMOTE_DIR} && docker compose ps"
        ;;

    logs)
        HOST="${1:-$(get_ip)}"
        if [ -z "$HOST" ]; then echo "No droplet IP. Run create or setup first."; exit 1; fi
        ssh_run "$HOST" "cd ${REMOTE_DIR} && docker compose logs -f --tail 100"
        ;;

    ssh)
        HOST="${1:-$(get_ip)}"
        if [ -z "$HOST" ]; then echo "No droplet IP. Run create or setup first."; exit 1; fi
        ssh "${SSH_USER}@${HOST}"
        ;;

    destroy)
        echo "==> Destroying droplet: $DROPLET_NAME..."
        doctl compute droplet delete "$DROPLET_NAME" --force
        rm -f "$IP_FILE"
        echo "==> Droplet destroyed."
        ;;

    env)
        # Push updated .env to server without redeploying
        HOST="${1:-$(get_ip)}"
        if [ -z "$HOST" ]; then echo "No droplet IP."; exit 1; fi
        if [ ! -f .env ]; then echo "No .env file found."; exit 1; fi
        scp -o StrictHostKeyChecking=accept-new .env "${SSH_USER}@${HOST}:${REMOTE_DIR}/.env"
        ssh_run "$HOST" "chmod 600 ${REMOTE_DIR}/.env"
        echo "==> .env updated. Run ./deploy.sh to restart with new values."
        ;;

    *)
        echo "Usage:"
        echo "  ./deploy.sh create              # Create droplet + deploy"
        echo "  ./deploy.sh setup <ip>           # Setup existing server"
        echo "  ./deploy.sh [<ip>]               # Deploy update"
        echo "  ./deploy.sh env                  # Push .env update"
        echo "  ./deploy.sh logs                 # Tail logs"
        echo "  ./deploy.sh ssh                  # SSH into droplet"
        echo "  ./deploy.sh destroy              # Tear down droplet"
        ;;
esac
