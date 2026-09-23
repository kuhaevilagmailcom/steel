#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_NAME="mishka-business-bot"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

cd "$APP_DIR"

sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip

python3 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
deactivate

chmod +x "$APP_DIR/run_bot.sh"

sudo tee "$SERVICE_FILE" >/dev/null <<SERVICE
[Unit]
Description=Mishka Telegram Business Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/.venv/bin/python $APP_DIR/run_bot_entry.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
SERVICE

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
sudo systemctl restart "$SERVICE_NAME"
sudo systemctl status "$SERVICE_NAME" --no-pager
