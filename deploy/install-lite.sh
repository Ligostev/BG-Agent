#!/usr/bin/env bash
# Облегчённая установка кабинета на чистый Ubuntu 22.04/24.04 (от 512 МБ памяти):
# без Docker, PostgreSQL и Redis — uvicorn под systemd, база SQLite, HTTPS через Caddy.
#
#   curl -fsSL https://raw.githubusercontent.com/Ligostev/BG-Agent/main/deploy/install-lite.sh | sudo bash -s lk.example.ru
#
# Репозиторий приватный, поэтому нужен GitHub-токен с правом чтения (Contents: Read):
#
#   T=<токен>; curl -fsSL -H "Authorization: token $T" \
#     https://raw.githubusercontent.com/Ligostev/BG-Agent/main/deploy/install-lite.sh \
#     | sudo REPO="https://$T@github.com/Ligostev/BG-Agent.git" bash -s lk.example.ru
#
# Повторный запуск обновляет код и перезапускает кабинет, данные и ключ сохраняются.
set -euo pipefail

DOMAIN="${1:?укажите домен, например: bash install-lite.sh lk.example.ru}"
REPO="${REPO:-https://github.com/Ligostev/BG-Agent.git}"
HOME_DIR=/opt/bg-agent
SRC="$HOME_DIR/app-src"

[ "$(id -u)" -eq 0 ] || { echo "Запустите от root (sudo)"; exit 1; }

echo "== Файл подкачки"
if ! swapon --show | grep -q .; then
  fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
  grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

echo "== Пакеты"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q git python3-venv caddy

echo "== Код"
id bg >/dev/null 2>&1 || useradd --system --create-home --home-dir "$HOME_DIR" bg
if [ -d "$SRC/.git" ]; then
  sudo -u bg git -C "$SRC" pull -q
else
  sudo -u bg git clone -q "$REPO" "$SRC"
fi
[ -x "$HOME_DIR/venv/bin/python" ] || sudo -u bg python3 -m venv "$HOME_DIR/venv"
sudo -u bg "$HOME_DIR/venv/bin/pip" install -q -r "$SRC/requirements.txt"
sudo -u bg mkdir -p "$HOME_DIR/data"

echo "== Настройки"
if [ ! -f "$HOME_DIR/env" ]; then
  cat > "$HOME_DIR/env" <<ENV
DATABASE_URL=sqlite+aiosqlite:///$HOME_DIR/data/bg.db
SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
COOKIE_SECURE=1
CAPTCHA_TIMEOUT=180
ENV
  chown bg "$HOME_DIR/env" && chmod 600 "$HOME_DIR/env"
fi

echo "== Служба"
cat > /etc/systemd/system/bg-agent.service <<UNIT
[Unit]
Description=BG-Agent: кабинет проверки компаний
After=network-online.target

[Service]
User=bg
WorkingDirectory=$SRC
EnvironmentFile=$HOME_DIR/env
# Без Redis проверки идут внутри процесса, поэтому воркер ровно один.
ExecStart=$HOME_DIR/venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --proxy-headers
Restart=always

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable -q bg-agent
systemctl restart bg-agent

echo "== HTTPS"
cat > /etc/caddy/Caddyfile <<CADDY
$DOMAIN {
	encode gzip
	reverse_proxy 127.0.0.1:8000
}
CADDY
systemctl reload caddy

sleep 3
if curl -fsS http://127.0.0.1:8000/healthz >/dev/null; then
  echo "Готово: https://$DOMAIN (сертификат появится, когда DNS-запись домена укажет на этот сервер)"
else
  echo "Кабинет не ответил, журнал: journalctl -u bg-agent -n 50"; exit 1
fi
