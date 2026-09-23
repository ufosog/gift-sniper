#!/usr/bin/env bash
# Run on the server (Ubuntu 22.04/24.04) as root, in the folder with gift-sniper-pack.tar.gz:
#   sudo bash install.sh
set -euo pipefail
APP=/opt/gift-sniper
apt-get update -q
apt-get install -y -q python3 python3-venv python3-pip sqlite3 tar
id gs >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin gs
mkdir -p "$APP"
systemctl stop gift-sniper 2>/dev/null || true
tar -xzf gift-sniper-pack.tar.gz -C "$APP"
python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install -q -r "$APP/gift_sniper/requirements.txt"
mkdir -p "$APP/logs" "$APP/reports" "$APP/secrets"
chown -R gs:gs "$APP"
chmod 600 "$APP/gift-sniper.env"
cp "$APP/deploy/gift-sniper.service" /etc/systemd/system/gift-sniper.service
systemctl daemon-reload
systemctl enable --now gift-sniper
sleep 5
systemctl --no-pager status gift-sniper | head -5
echo "Готово. Проверка: sudo -u gs $APP/venv/bin/python $APP/health.py"
