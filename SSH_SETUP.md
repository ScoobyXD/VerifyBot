# Raspberry Pi SSH bootstrap (first step)

This repository now includes `pi_ssh_bootstrap.py` to do the first hardware step:
create a simple markdown file on your Raspberry Pi over SSH.

## 1) Store credentials locally (gitignored)

1. Copy `pi_ssh.env.example` to `.secrets/pi_ssh.env`
2. Fill in `PI_HOST`, `PI_USER`, `PI_PASSWORD`

`.secrets/` is ignored by git so credentials are not committed.

## 2) Run

```bash
python pi_ssh_bootstrap.py
```

Default behavior writes:

- Remote file: `~/Documents/verifybot_hello.md`
- Contents: `# hi`

## 3) Optional overrides

```bash
python pi_ssh_bootstrap.py \
  --host ScoobyXD \
  --user scoobyxd \
  --password 'your-password' \
  --remote-path Documents/hello.md \
  --message '# hi from VerifyBot\n'
```

## Notes

- If using password auth, the script expects `sshpass` to be installed.
- If you already have SSH keys set up, omit password and key auth will be used.
