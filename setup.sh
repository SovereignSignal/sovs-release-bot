#!/bin/bash
# Setup script for OpenClaw Release Bot
# Run on the target server after cloning the repo

set -euo pipefail

BOT_DIR="/opt/sovs-release-bot"
SERVICE_NAME="sovs-release-bot"

echo "Setting up OpenClaw Release Bot..."

# Create service user if needed
if ! id releasebot >/dev/null 2>&1; then
    sudo useradd --system --home "$BOT_DIR" --shell /usr/sbin/nologin releasebot
fi

# Create directory
sudo mkdir -p "$BOT_DIR/data"
sudo cp bot.py "$BOT_DIR/"
sudo chmod +x "$BOT_DIR/bot.py"
sudo chown -R releasebot:releasebot "$BOT_DIR"

# Create .env if it doesn't exist
if [[ ! -f "$BOT_DIR/.env" ]]; then
    echo "Creating .env file. Fill in your values:"
    sudo tee "$BOT_DIR/.env" > /dev/null << 'ENVEOF'
TELEGRAM_BOT_TOKEN=your-bot-token-here
TELEGRAM_CHAT_ID=your-chat-id-here
CHECK_INTERVAL=30
STATE_FILE=/opt/sovs-release-bot/data/last-version.txt
STATE_DIR=/opt/sovs-release-bot/data
WATCH_PACKAGES=openclaw,hermes-agent,codex,claude-code
ENVEOF
    sudo chmod 600 "$BOT_DIR/.env"
    sudo chown releasebot:releasebot "$BOT_DIR/.env"
    echo "Edit $BOT_DIR/.env with your actual values"
fi

# Create systemd service
sudo cp deploy/systemd/${SERVICE_NAME}.service /etc/systemd/system/${SERVICE_NAME}.service

sudo systemctl daemon-reload
sudo systemctl enable ${SERVICE_NAME}

echo ""
echo "Setup complete."
echo ""
echo "Next steps:"
echo "  1. Edit $BOT_DIR/.env with your Telegram bot token and chat ID"
echo "  2. Start the bot: sudo systemctl start ${SERVICE_NAME}"
echo "  3. Test: sudo systemctl status ${SERVICE_NAME}"
echo "  4. Logs: journalctl -u ${SERVICE_NAME} -f"
