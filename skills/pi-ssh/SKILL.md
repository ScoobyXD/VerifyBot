---
name: pi-ssh
description: SSH from Windows (or bash shells) into a Linux Raspberry Pi to create/update files after raw_md -> programs extraction, using local secret config files that are gitignored.
---

# pi-ssh

Use this skill to SSH/SCP into a Raspberry Pi and update files safely without hardcoding credentials in scripts.

## Security rule
Never put real `PI_USER`, `PI_HOST`, or `PI_PASSWORD` in tracked files.

Store them only in a local file:
- `.secrets/pi_ssh.env` (gitignored)
- Start by copying: `skills/pi-ssh/assets/pi_ssh.env.example`

## Setup
1. Create local secret file:
   - `cp skills/pi-ssh/assets/pi_ssh.env.example .secrets/pi_ssh.env`
2. Edit `.secrets/pi_ssh.env` with your real values.
3. On Windows (PowerShell), test manual SSH first:
   - `ssh <PI_USER>@<PI_HOST>`
   - Enter password when prompted.

## First action: create proof file on Pi
From a bash-like shell (Git Bash/WSL/Linux/macOS), run:

```bash
bash skills/pi-ssh/scripts/pi_hello_ssh.sh
```

What it does remotely:
- creates `~/hello_ssh.md`
- writes `hello i ssh'd here`
- prints file details and contents

## Why the previous run may have looked like "it did nothing"
- SSH can fail before file creation (bad host/user/password/network)
- Hostname may not resolve from your laptop (use Pi IP if needed)
- Script may not have been executed from repo root
- Password auth requires prompt unless `sshpass` is installed and `PI_PASSWORD` is set

## Update extracted files (`programs/`) later
PowerShell examples:

```powershell
scp .\programs\pi_can_send.py <PI_USER>@<PI_HOST>:/home/<PI_USER>/programs/pi_can_send.py
ssh <PI_USER>@<PI_HOST> "python3 /home/<PI_USER>/programs/pi_can_send.py"
```
