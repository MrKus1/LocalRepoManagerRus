#!/bin/bash
# Офлайн-установка Repo Manager на Oracle Linux 10
# Ничего не качает из интернета (кроме того, что уже в системных репо OL)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
INSTALL_DIR="${INSTALL_DIR:-/opt/repo-manager}"
DATA_DIR="${DATA_DIR:-/var/lib/repo-manager}"
SERVICE_USER="${SERVICE_USER:-repoman}"

echo "=========================================="
echo "  Repo Manager — офлайн установка"
echo "=========================================="
echo "Исходники:  $ROOT_DIR"
echo "Установка:  $INSTALL_DIR"
echo "Данные:     $DATA_DIR"
echo ""

# --- Проверки ---
if [[ $EUID -ne 0 ]]; then
    echo "Запусти от root: sudo $0"
    exit 1
fi

if [[ ! -f "$ROOT_DIR/requirements.txt" ]]; then
    echo "Ошибка: не найден requirements.txt"
    exit 1
fi

if [[ ! -d "$ROOT_DIR/wheels" ]] || [[ -z "$(ls -A "$ROOT_DIR/wheels" 2>/dev/null)" ]]; then
    echo "Ошибка: папка wheels пуста. Сначала выполни scripts/build-offline.sh на машине с интернетом."
    exit 1
fi

# --- Системные пакеты (из локальных репо OL10, без интернета если зеркало есть) ---
echo "==> Установка системных пакетов (dnf)"
dnf install -y \
    python3.12 python3.12-pip python3.12-devel \
    dnf-plugins-core \
    createrepo_c \
    rsync \
    postgresql-server postgresql-contrib \
    nginx \
    2>/dev/null || dnf install -y \
    python3 python3-pip python3-devel \
    dnf-plugins-core createrepo_c rsync \
    postgresql-server postgresql-contrib nginx

# Определяем python
PYTHON=$(command -v python3.12 || command -v python3)
echo "Python: $PYTHON ($($PYTHON --version))"

# --- Пользователь ---
if ! id "$SERVICE_USER" &>/dev/null; then
    echo "==> Создание пользователя $SERVICE_USER"
    useradd -r -s /sbin/nologin -d "$DATA_DIR" "$SERVICE_USER"
fi

# --- Директории ---
echo "==> Создание директорий"
mkdir -p "$INSTALL_DIR"
mkdir -p "$DATA_DIR"/{mirrors,local,uploads,logs,public,aptly}
mkdir -p /var/lib/aptly

# Копируем приложение
echo "==> Копирование приложения в $INSTALL_DIR"
rsync -a --delete \
    --exclude 'wheels' \
    --exclude 'storage' \
    --exclude '.git' \
    --exclude '__pycache__' \
    --exclude '*.pyc' \
    --exclude 'venv' \
    "$ROOT_DIR/" "$INSTALL_DIR/"

# aptly
if [[ -f "$ROOT_DIR/bin/aptly" ]]; then
    echo "==> Установка aptly"
    cp "$ROOT_DIR/bin/aptly" /usr/local/bin/aptly
    chmod 755 /usr/local/bin/aptly
    /usr/local/bin/aptly version || true
else
    echo "ВНИМАНИЕ: bin/aptly не найден — deb-зеркала работать не будут"
fi

# Конфиг aptly
if [[ ! -f /etc/aptly.conf ]]; then
    cat > /etc/aptly.conf << 'APTEOF'
{
  "rootDir": "/var/lib/repo-manager/aptly",
  "downloadConcurrency": 4,
  "downloadSpeedLimit": 0,
  "architectures": [],
  "dependencyFollowSuggests": false,
  "dependencyFollowRecommends": false,
  "dependencyFollowAllVariants": false,
  "dependencyFollowSource": false,
  "gpgDisableSign": true,
  "gpgDisableVerify": true,
  "downloadSourcePackages": false,
  "skipLegacyPool": true,
  "ppaDistributorID": "ubuntu",
  "ppaCodename": "",
  "skipContentsPublishing": false,
  "FileSystemPublishEndpoints": {
    "default": {
      "rootDir": "/var/lib/repo-manager/public",
      "linkMethod": "hardlink"
    }
  }
}
APTEOF
fi

# --- Python venv + офлайн pip ---
echo "==> Создание venv и установка Python-пакетов (офлайн)"
$PYTHON -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install --upgrade pip --no-index --find-links="$ROOT_DIR/wheels" 2>/dev/null || \
    "$INSTALL_DIR/venv/bin/pip" install --upgrade pip

"$INSTALL_DIR/venv/bin/pip" install \
    --no-index \
    --find-links="$ROOT_DIR/wheels" \
    -r "$INSTALL_DIR/requirements.txt"

# --- .env ---
if [[ ! -f "$INSTALL_DIR/.env" ]]; then
    echo "==> Создание .env"
    cp "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env" 2>/dev/null || cat > "$INSTALL_DIR/.env" << 'ENVEOF'
POSTGRES_USER=repoman
POSTGRES_PASSWORD=changeme
POSTGRES_HOST=127.0.0.1
POSTGRES_PORT=5432
POSTGRES_DB=repo_manager
DEBUG=false
ENVEOF
    echo "  Отредактируй $INSTALL_DIR/.env — укажи пароль PostgreSQL!"
fi

# --- PostgreSQL (если ещё не настроен) ---
if ! systemctl is-active --quiet postgresql 2>/dev/null; then
    echo "==> Инициализация PostgreSQL"
    postgresql-setup --initdb 2>/dev/null || true
    systemctl enable --now postgresql
fi

# Создаём БД/пользователя если нет
sudo -u postgres psql -tc "SELECT 1 FROM pg_roles WHERE rolname='repoman'" | grep -q 1 || {
    echo "==> Создание пользователя и БД PostgreSQL"
    # Пароль из .env или changeme
    PG_PASS=$(grep POSTGRES_PASSWORD "$INSTALL_DIR/.env" | cut -d= -f2 | tr -d ' "' || echo changeme)
    sudo -u postgres psql << SQL
CREATE USER repoman WITH PASSWORD '${PG_PASS}';
CREATE DATABASE repo_manager OWNER repoman;
GRANT ALL PRIVILEGES ON DATABASE repo_manager TO repoman;
SQL
    sudo -u postgres psql -d repo_manager -c "GRANT ALL ON SCHEMA public TO repoman; ALTER SCHEMA public OWNER TO repoman;" 2>/dev/null || true
}

# --- Права ---
chown -R "$SERVICE_USER:$SERVICE_USER" "$DATA_DIR"
chown -R "$SERVICE_USER:$SERVICE_USER" /var/lib/aptly
chown -R root:root "$INSTALL_DIR"
# venv и .env должен читать сервис
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR/venv" 2>/dev/null || true
chown "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR/.env"

# --- systemd ---
echo "==> Установка systemd unit"
cat > /etc/systemd/system/repo-manager.service << EOF
[Unit]
Description=Repo Manager (deb/rpm mirrors)
After=network.target postgresql.service
Wants=postgresql.service

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_USER
WorkingDirectory=$INSTALL_DIR
Environment=PATH=$INSTALL_DIR/venv/bin:/usr/local/bin:/usr/bin
EnvironmentFile=$INSTALL_DIR/.env
ExecStart=$INSTALL_DIR/venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable repo-manager

echo "==> Nginx"
cp "$INSTALL_DIR/scripts/nginx-repo-manager.conf" /etc/nginx/conf.d/repo-manager.conf
nginx -t && systemctl enable --now nginx && systemctl reload nginx || echo "ВНИМАНИЕ: nginx не запустился"

PG_PASS=$(grep ^POSTGRES_PASSWORD= "$INSTALL_DIR/.env" | cut -d= -f2- | tr -d ' "' || echo changeme)
echo ""
echo "=========================================="
echo "  Установка завершена"
echo "=========================================="
echo "Пароль PostgreSQL из .env: ${PG_PASS}"
echo "Файл: $INSTALL_DIR/.env"
if grep -E 'ident|peer' /var/lib/pgsql/data/pg_hba.conf 2>/dev/null | grep -v '^#' >/dev/null; then
  echo "ВНИМАНИЕ: в pg_hba.conf ещё есть ident/peer."
  echo "Замени на scram-sha-256 и выполни: systemctl restart postgresql"
else
  echo "pg_hba.conf: ident/peer не найден."
fi
echo "Интерфейс: http://$(hostname -I | awk '{print $1}'):8000"
echo "Пакеты:    http://$(hostname -I | awk '{print $1}')/repo/rpm/ и /repo/deb/"
echo "Запуск:    systemctl start repo-manager"
