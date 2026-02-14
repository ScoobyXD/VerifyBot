#!/usr/bin/env bash
set -euo pipefail

# Sync only the Pi bootstrap files from a source branch into a fresh branch off origin/main.
# This avoids repeated conflict-heavy PRs when the same files are edited many times.

SOURCE_BRANCH="${1:-work}"
TARGET_BRANCH="sync/pi-bootstrap-$(date +%Y%m%d-%H%M%S)"

FILES=(
  ".gitignore"
  "SSH_SETUP.md"
  "pi_ssh.env.example"
  "pi_ssh_bootstrap.py"
)

if ! git rev-parse --git-dir >/dev/null 2>&1; then
  echo "[ERROR] Not inside a git repository"
  exit 1
fi

if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "[ERROR] Working tree is not clean. Commit/stash first."
  exit 1
fi

if ! git show-ref --verify --quiet "refs/heads/${SOURCE_BRANCH}"; then
  echo "[ERROR] Source branch '${SOURCE_BRANCH}' does not exist locally."
  exit 1
fi

if ! git remote get-url origin >/dev/null 2>&1; then
  echo "[ERROR] 'origin' remote is not configured."
  echo "        Example: git remote add origin git@github.com:ScoobyXD/VerifyBot.git"
  exit 1
fi

echo "[1/6] Fetching origin/main..."
git fetch origin main

echo "[2/6] Creating ${TARGET_BRANCH} from origin/main..."
git checkout -b "${TARGET_BRANCH}" origin/main

echo "[3/6] Copying files from ${SOURCE_BRANCH}..."
git checkout "${SOURCE_BRANCH}" -- "${FILES[@]}"

echo "[4/6] Committing sync patch..."
git add "${FILES[@]}"
git commit -m "Sync Pi SSH bootstrap files from ${SOURCE_BRANCH}"

echo "[5/6] Pushing ${TARGET_BRANCH}..."
git push -u origin "${TARGET_BRANCH}"

echo "[6/6] Done. Open a PR: ${TARGET_BRANCH} -> main"
