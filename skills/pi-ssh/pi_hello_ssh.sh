#!/usr/bin/env bash
set -euo pipefail

SECRETS_FILE=".secrets/pi_ssh.env"

if [[ ! -f "$SECRETS_FILE" ]]; then
  echo "[error] Missing $SECRETS_FILE" >&2
  echo "Run the one-time setup command from skills/pi-ssh/SKILL.md" >&2
  exit 1
fi

# shellcheck disable=SC1090
source "$SECRETS_FILE"

: "${PI_USER:?Set PI_USER in $SECRETS_FILE}"
: "${PI_HOST:?Set PI_HOST in $SECRETS_FILE}"

REMOTE_CMD="printf '%s\n' \"hello i ssh'd here\" > ~/hello_ssh.md && ls -l ~/hello_ssh.md && cat ~/hello_ssh.md"

if command -v sshpass >/dev/null 2>&1 && [[ -n "${PI_PASSWORD:-}" ]]; then
  SSHPASS="$PI_PASSWORD" sshpass -e ssh -o StrictHostKeyChecking=accept-new "${PI_USER}@${PI_HOST}" "$REMOTE_CMD"
else
  ssh -o StrictHostKeyChecking=accept-new "${PI_USER}@${PI_HOST}" "$REMOTE_CMD"
fi
