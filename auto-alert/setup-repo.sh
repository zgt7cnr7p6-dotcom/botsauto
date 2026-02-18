#!/bin/bash
# Setup script: creates the 'auto-alert' private repo on GitHub
# and pushes all files.
#
# Usage: ./setup-repo.sh
#
# Vereisten:
#   - gh CLI geinstalleerd en ingelogd (gh auth login)
#   - TELEGRAM_BOT_TOKEN en TELEGRAM_CHAT_ID beschikbaar

set -euo pipefail

REPO_NAME="auto-alert"
GITHUB_USER=$(gh api user -q '.login')

echo "=== Auto-Alert Repo Setup ==="
echo "GitHub user: $GITHUB_USER"

# 1. Maak private repo
echo ">> Private repo '$REPO_NAME' aanmaken..."
gh repo create "$REPO_NAME" --private --description "Geautomatiseerde Audi Q3 45 TFSI e scraper met Telegram alerts" || {
    echo "Repo bestaat mogelijk al, doorgaan..."
}

# 2. Init git en push
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Kopieer bestanden naar temp dir voor clean push
TEMP_DIR=$(mktemp -d)
cp -r scraper.py requirements.txt README.md .gitignore "$TEMP_DIR/"
mkdir -p "$TEMP_DIR/.github/workflows"
cp .github/workflows/alert.yml "$TEMP_DIR/.github/workflows/"

cd "$TEMP_DIR"
git init
git branch -m main
git add -A
git commit -m "Initial commit: Auto-Alert scraper"
git remote add origin "https://github.com/$GITHUB_USER/$REPO_NAME.git"
git push -u origin main

# 3. Set secrets
echo ""
echo ">> Secrets instellen..."
if [ -n "${TELEGRAM_BOT_TOKEN:-}" ]; then
    echo "$TELEGRAM_BOT_TOKEN" | gh secret set TELEGRAM_BOT_TOKEN --repo "$GITHUB_USER/$REPO_NAME"
    echo "   TELEGRAM_BOT_TOKEN ingesteld"
else
    echo "   TELEGRAM_BOT_TOKEN niet gevonden in environment, stel handmatig in:"
    echo "   gh secret set TELEGRAM_BOT_TOKEN --repo $GITHUB_USER/$REPO_NAME"
fi

if [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
    echo "$TELEGRAM_CHAT_ID" | gh secret set TELEGRAM_CHAT_ID --repo "$GITHUB_USER/$REPO_NAME"
    echo "   TELEGRAM_CHAT_ID ingesteld"
else
    echo "   TELEGRAM_CHAT_ID niet gevonden in environment, stel handmatig in:"
    echo "   gh secret set TELEGRAM_CHAT_ID --repo $GITHUB_USER/$REPO_NAME"
fi

# 4. Trigger workflow
echo ""
echo ">> Workflow handmatig triggeren..."
sleep 2
gh workflow run alert.yml --repo "$GITHUB_USER/$REPO_NAME" || {
    echo "   Workflow kon niet direct getriggerd worden."
    echo "   Ga naar: https://github.com/$GITHUB_USER/$REPO_NAME/actions"
    echo "   en klik 'Run workflow'"
}

# Cleanup
rm -rf "$TEMP_DIR"

echo ""
echo "=== Setup compleet ==="
echo "Repo: https://github.com/$GITHUB_USER/$REPO_NAME"
echo "Actions: https://github.com/$GITHUB_USER/$REPO_NAME/actions"
echo ""
echo "BELANGRIJK: Revoke je Telegram bot token via @BotFather (/revoke)"
echo "en maak een nieuwe aan, want het huidige token is gedeeld in een chat!"
