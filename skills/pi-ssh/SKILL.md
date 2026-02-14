---
name: pi-ssh
description: Simple Raspberry Pi SSH helper that keeps user/host/password in .secrets/pi_ssh.env and creates ~/hello_ssh.md on the Pi.
---

# pi-ssh

## One-time setup + run (single command)
Copy/paste this command from repo root, then replace the `<...>` values:

```bash
mkdir -p .secrets && printf "PI_USER=<YOUR_PI_USER>\nPI_HOST=<YOUR_PI_HOST_OR_IP>\nPI_PASSWORD=<YOUR_PI_PASSWORD>\n" > .secrets/pi_ssh.env && bash skills/pi-ssh/pi_hello_ssh.sh
```

## Files
- Script: `skills/pi-ssh/pi_hello_ssh.sh`
- Secret template: `skills/pi-ssh/pi_ssh.env.example`
- Local secret file (gitignored): `.secrets/pi_ssh.env`
