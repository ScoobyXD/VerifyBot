#!/usr/bin/env bash
set -euo pipefail

# Usage:
#   1) Copy skills/pi-ssh/assets/pi_ssh.env.example -> .secrets/pi_ssh.env
#   2) Fill PI_USER / PI_HOST / PI_PASSWORD in that local file
#   3) Run this script from repo root (Git Bash/WSL/Linux/macOS)

SECRETS_FILE="${PI_SSH_SECRETS_FILE:-.secrets/pi_ssh.env}"

if [[ -f "$SECRETS_FILE" ]]; then
  # shellcheck disable=SC1090
  source "$SECRETS_FILE"
else
  echo "[error] Secrets file not found: $SECRETS_FILE" >&2
  echo "Create it from skills/pi-ssh/assets/pi_ssh.env.example first." >&2
  exit 1
fi

: "${PI_USER:?PI_USER is required in $SECRETS_FILE}"
: "${PI_HOST:?PI_HOST is required in $SECRETS_FILE}"

REMOTE_CMD="mkdir -p ~/programs && printf '%s\n' \"hello i ssh'd here\" > ~/hello_ssh.md && ls -l ~/hello_ssh.md && cat ~/hello_ssh.md"

if [[ -n "${PI_PASSWORD:-}" ]]; then
  if command -v sshpass >/dev/null 2>&1; then
    SSHPASS="$PI_PASSWORD" sshpass -e ssh -o StrictHostKeyChecking=accept-new "${PI_USER}@${PI_HOST}" "$REMOTE_CMD"
  else
    echo "[warn] PI_PASSWORD is set but sshpass is not installed; falling back to interactive password prompt." >&2
    ssh -o StrictHostKeyChecking=accept-new "${PI_USER}@${PI_HOST}" "$REMOTE_CMD"
  fi
else
  echo "[info] PI_PASSWORD is empty; SSH will prompt you for password interactively." >&2
  ssh -o StrictHostKeyChecking=accept-new "${PI_USER}@${PI_HOST}" "$REMOTE_CMD"
fi
