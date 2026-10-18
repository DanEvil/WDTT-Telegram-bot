#!/bin/bash
set -e

# Project directory
PROJECT_DIR="/opt/wdtt-bot"
SERVICE_NAME="wdtt-bot"

echo "--- 1. Updating system and installing dependencies ---"
sudo apt update && sudo apt install -y git python3-pip python3-venv build-essential libffi-dev libssl-dev

# Remove existing project if it exists to ensure a clean install
if [ -d "$PROJECT_DIR" ]; then
    echo "Project directory exists. Removing for clean install..."
    sudo rm -rf "$PROJECT_DIR"
fi

echo "--- 2. Cloning repository ---"
git clone https://github.com/DanEvil/WDTT-Telegram-bot.git "$PROJECT_DIR"
cd "$PROJECT_DIR"

echo "--- 3. Setting up virtual environment ---"
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

echo "--- 4. Interactive Configuration ---"
echo "Please enter the data for the bot:"
read -p "TELEGRAM_BOT_TOKEN: " token
read -p "ADMIN_IDS (comma-separated): " admin_ids
read -p "YOOKASSA_SHOP_ID: " yk_shop
read -p "YOOKASSA_SECRET_KEY: " yk_secret
read -p "RETURN_URL (e.g., https://t.me/bot): " return_url
read -p "WDTT_BASE_URL (e.g., https://ip:port/wdtt): " wdtt_url
read -p "WDTT_USERNAME: " wdtt_user
read -p "WDTT_PASSWORD: " wdtt_pass
read -p "Enter plans in JSON format (or leave empty for defaults): " plans_json

# Create .env file
cat <<EOF > .env
TELEGRAM_BOT_TOKEN=$token
ADMIN_IDS=$admin_ids
YOOKASSA_SHOP_ID=$yk_shop
YOOKASSA_SECRET_KEY=$yk_secret
RETURN_URL=$return_url
WDTT_BASE_URL=$wdtt_url
WDTT_USERNAME=$wdtt_user
WDTT_PASSWORD=***
EOF

# Create plans.json
if [ -n "$plans_json" ]; then
    echo "$plans_json" > plans.json
else
    echo '[{"id": "week", "name": "Неделя", "days": 7, "price": 99, "traffic_gb": 20, "max_devices": 1}, {"id": "month", "name": "Месяц", "days": 30, "price": 249, "traffic_gb": 100, "max_devices": 2}, {"id": "year", "name": "Год", "days": 365, "price": 1990, "traffic_gb": 0, "max_devices": 5}]' > plans.json
fi

echo "--- 5. Configuring auto-launch (Systemd) ---"
# Create the systemd service file
sudo bash -c "cat <<EOF > /etc/systemd/system/$SERVICE_NAME.service
[Unit]
Description=WDTT Telegram Bot
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$PROJECT_DIR
Environment=\"PATH=$PROJECT_DIR/.venv/bin\"
ExecStart=$PROJECT_DIR/.venv/bin/python $PROJECT_DIR/bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF"

echo "--- 6. Starting service ---"
sudo systemctl daemon-reload
sudo systemctl enable $SERVICE_NAME
sudo systemctl start $SERVICE_NAME

echo "-----------------------------------------------------------"
echo "DEPLOYMENT COMPLETE!"
echo "1. Create the configuration file:"
echo "   nano $PROJECT_DIR/.env"
echo "2. Fill it with your data (tokens, YuKassa keys, Panel IP)."
echo "3. Check bot status:"
echo "   sudo systemctl status $SERVICE_NAME"
echo "-----------------------------------------------------------"
