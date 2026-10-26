#!/usr/bin/env bash
set -euo pipefail
if [ "$(id -u)" -ne 0 ]; then
  echo "Run: sudo bash install.sh"
  exit 1
fi
source_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
install_dir=/opt/ripcars-post
apt-get update
apt-get install -y python3 python3-venv
if ! id ripcars-post >/dev/null 2>&1; then
  useradd --system --home-dir /var/lib/ripcars-post --shell /usr/sbin/nologin ripcars-post
fi
install -d -m 755 "$install_dir"
install -d -o ripcars-post -g ripcars-post -m 700 /var/lib/ripcars-post
for name in bot.py core.py requirements.txt; do
  install -m 644 "$source_dir/$name" "$install_dir/$name"
done
python3 -m venv "$install_dir/venv"
"$install_dir/venv/bin/pip" install -r "$install_dir/requirements.txt"
if [ ! -f /etc/ripcars-post.env ]; then
  python3 - <<'PY'
import getpass
import os

def secret(prompt):
    value = getpass.getpass(prompt).strip()
    if not value or any(c in value for c in '\r\n\x00'):
        raise SystemExit('Empty or invalid credential. Run the installer again.')
    return value

token = secret('Discord bot token (hidden): ')
api_key = secret('TwitterAPI.io API key (hidden): ')
print('Discord server ID: ', end='', flush=True)
with open('/dev/tty', 'r') as terminal:
    guild = terminal.readline().strip()
if not guild.isdigit():
    raise SystemExit('Enter the numeric Discord server ID. Run the installer again.')
values = {'DISCORD_BOT_TOKEN': token, 'TWITTERAPI_KEY': api_key,
          'DISCORD_GUILD_ID': guild, 'DATABASE_PATH': '/var/lib/ripcars-post/feed.sqlite3'}
fd = os.open('/etc/ripcars-post.env', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as target:
    for key, value in values.items():
        value = value.replace('\\', '\\\\').replace('"', '\\"')
        target.write(f'{key}="{value}"\n')
print('Credentials saved.')
PY
fi
cat > /etc/systemd/system/ripcars-post.service <<'SERVICE'
[Unit]
Description=Ripcars Post Discord feed
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
User=ripcars-post
Group=ripcars-post
WorkingDirectory=/opt/ripcars-post
EnvironmentFile=/etc/ripcars-post.env
ExecStart=/opt/ripcars-post/venv/bin/python /opt/ripcars-post/bot.py
Restart=on-failure
RestartSec=15
TimeoutStopSec=30
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/ripcars-post

[Install]
WantedBy=multi-user.target
SERVICE
systemctl daemon-reload
systemctl enable --now ripcars-post
systemctl restart ripcars-post
echo "Installed. Run /setup in your Discord server."
echo "Status: sudo systemctl status ripcars-post --no-pager"
echo "Logs: sudo journalctl -u ripcars-post -n 50 --no-pager"
