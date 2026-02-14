---
name: pi-ssh
description: SSH from a Windows laptop into a Linux Raspberry Pi, then update files after code extraction (raw_md -> programs). Includes a first-run command to create a hello markdown file on the Pi.
---

# pi-ssh

Use this skill when the task is to connect from Windows to Raspberry Pi over SSH and edit or create files remotely.

## Inputs
- Pi user: `scoobyxd`
- Pi host: `ScoobyXD`
- Local flow context: extracted files move from `raw_md` into `programs`

## Workflow
1. Confirm SSH client on Windows:
   - `ssh -V`
2. Connect:
   - `ssh scoobyxd@ScoobyXD`
   - Enter the password when prompted.
3. First test action (create hello file on Pi):
   - `echo "hello i ssh'd here" > ~/hello_ssh.md`
4. Verify:
   - `cat ~/hello_ssh.md`

## Non-interactive one-liner from Windows
Use this only when you want a single command that creates the file immediately after login:

```powershell
ssh scoobyxd@ScoobyXD "echo 'hello i ssh''d here' > ~/hello_ssh.md && cat ~/hello_ssh.md"
```

## Updating extracted files later
After extraction to local `programs/`, upload one file with SCP:

```powershell
scp .\programs\pi_can_send.py scoobyxd@ScoobyXD:/home/scoobyxd/programs/pi_can_send.py
```

Then run it remotely:

```powershell
ssh scoobyxd@ScoobyXD "python3 /home/scoobyxd/programs/pi_can_send.py"
```

## Optional helper script
Use `scripts/pi_hello_ssh.sh` from this skill if you are already in a bash-like shell (Git Bash/WSL/macOS/Linux).
