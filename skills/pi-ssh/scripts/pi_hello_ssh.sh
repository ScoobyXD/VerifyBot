#!/usr/bin/env bash
set -euo pipefail

PI_USER="${PI_USER:-scoobyxd}"
PI_HOST="${PI_HOST:-ScoobyXD}"

ssh "${PI_USER}@${PI_HOST}" "echo 'hello i ssh'\''d here' > ~/hello_ssh.md && cat ~/hello_ssh.md"
