# Raspberry Pi SSH bootstrap (first step)

This repo includes `pi_ssh_bootstrap.py` to do the first real hardware step:
write a markdown file to your Raspberry Pi over SSH and read it back for verification.

## Important

`python -m py_compile pi_ssh_bootstrap.py` only checks Python syntax.
It does **not** connect to your Pi and does **not** create a remote file.

## 1) Store credentials locally (gitignored)

1. Copy `pi_ssh.env.example` to `.secrets/pi_ssh.env`
2. Fill in `PI_HOST`, `PI_USER`, `PI_PASSWORD`
   - `PI_HOST` can be either `ScoobyXD` or `scoobyxd@ScoobyXD`
   - If hostname does not resolve, use your Pi IP (example: `192.168.43.55`)

`.secrets/` is ignored by git so credentials are not committed.

## 2) Run the actual SSH write

```bash
python pi_ssh_bootstrap.py
```

Default remote target:
- `~/Documents/verifybot_hello.md`

Default contents:
- `# hi`

The script then verifies by running `ls` + `cat` on that remote file.

## 3) Optional overrides

```bash
python pi_ssh_bootstrap.py \
  --host 192.168.43.55 \
  --user scoobyxd \
  --password 'your-password' \
  --remote-path Documents/hello.md \
  --message '# hi from VerifyBot\n'
```

## Notes

- On Linux/macOS, password auth uses `sshpass` when available.
- On Windows (or any machine without `sshpass`), the script falls back to normal `ssh` and prompts for password interactively.
- If you already have SSH keys set up, omit password and key auth will be used.
