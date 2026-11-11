#!/usr/bin/env bash
# Fallback when YouTube blocks GitHub Actions IPs: run the same poll from your own computer.
# Needs: a clone of the repo with a git remote you can push to, .venv (or jonah-ledger on PATH), and a .env file
# holding ANTHROPIC_API_KEY and DISCORD_WEBHOOK_URL (copy .env.example). Schedule with cron or launchd (see README).
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -f .env ]; then set -a; . ./.env; set +a; fi
if [ -f .venv/bin/activate ]; then . .venv/bin/activate; fi

git pull --rebase --autostash --quiet || true     # pick up commits the Actions bot made
jonah-ledger ledger verify
jonah-ledger poll
jonah-ledger ledger verify

git add data reports
if git diff --cached --quiet; then echo "nothing new"; exit 0; fi
git commit -m "data: local poll $(date -u +%FT%TZ) [skip ci]"
git pull --rebase --autostash --quiet
git push
