#!/bin/bash
# Запускается от root через sudo. Останавливает службу, накладывает архив, стартует.
set -euo pipefail

ACTION="${1:-}"
ARCHIVE="${2:-}"
INSTALL_DIR="${INSTALL_DIR:-/opt/repo-manager}"
BACKUP_DIR="${BACKUP_DIR:-/opt/repo-manager.bak}"
LOG="${LOG:-/var/lib/repo-manager/logs/update.log}"
SERVICE_USER="${SERVICE_USER:-repoman}"

log() { echo "$(date '+%F %T') $*" | tee -a "$LOG"; }

# Отделяемся от службы, иначе stop убьёт и скрипт.
if [[ "${UPDATE_DETACHED:-}" != "1" && "$ACTION" != "install-helper" ]]; then
    export UPDATE_DETACHED=1
    setsid "$0" "$@" >>"$LOG" 2>&1 < /dev/null &
    exit 0
fi

if [[ $EUID -ne 0 ]]; then
    echo "Нужен root" >&2
    exit 1
fi
mkdir -p "$(dirname "$LOG")"

install_helper() {
    if [[ -f "$INSTALL_DIR/scripts/repo-manager-update.sh" ]]; then
        install -m 755 "$INSTALL_DIR/scripts/repo-manager-update.sh" /usr/local/sbin/repo-manager-update
    fi
    cat > /etc/sudoers.d/repo-manager-update << 'SUDO'
repoman ALL=(root) NOPASSWD: /usr/local/sbin/repo-manager-update
SUDO
    chmod 440 /etc/sudoers.d/repo-manager-update
}

apply() {
    if [[ ! -f "$ARCHIVE" ]]; then
        log "архив не найден: $ARCHIVE"
        exit 1
    fi
    if ! python3 - "$ARCHIVE" << 'PY'
import sys, tarfile
from pathlib import PurePosixPath
path = sys.argv[1]
with tarfile.open(path, "r:gz") as tar:
    for member in tar.getmembers():
        name = member.name
        parts = PurePosixPath(name).parts
        if member.issym() or member.islnk() or (not name) or name.startswith("/") or ".." in parts or not parts or parts[0] != "repo-manager":
            print(name)
            sys.exit(1)
PY
    then
        log "архив отклонён: внутри tar есть путь вне repo-manager"
        exit 1
    fi
    log "обновление из $ARCHIVE"
    systemctl stop repo-manager || true
    sleep 1
    rm -rf "$BACKUP_DIR"
    cp -a "$INSTALL_DIR" "$BACKUP_DIR"
    tmp="$(mktemp -d)"
    tar xzf "$ARCHIVE" -C "$tmp"
    src="$tmp/repo-manager"
    if [[ ! -d "$src" ]]; then
        log "в архиве нет repo-manager/"
        systemctl start repo-manager || true
        exit 1
    fi
    rsync -a --delete \
        --exclude .env \
        --exclude venv \
        --exclude storage \
        --exclude __pycache__ \
        --exclude '*.pyc' \
        "$src/" "$INSTALL_DIR/"
    if [[ -d "$INSTALL_DIR/wheels" ]] && [[ -n "$(ls -A "$INSTALL_DIR/wheels" 2>/dev/null)" ]] && [[ -x "$INSTALL_DIR/venv/bin/pip" ]]; then
        "$INSTALL_DIR/venv/bin/pip" install --no-index --find-links "$INSTALL_DIR/wheels" -r "$INSTALL_DIR/requirements.txt" || log "pip не обновил зависимости"
    fi
    chown -R root:root "$INSTALL_DIR"
    chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR/venv" "$INSTALL_DIR/.env" 2>/dev/null || true
    install_helper
    if [[ ! -f /var/lib/repo-manager/api-allow.conf && -f "$INSTALL_DIR/scripts/api-allow.conf" ]]; then
        cp "$INSTALL_DIR/scripts/api-allow.conf" /var/lib/repo-manager/api-allow.conf
    fi
    conf_src="$INSTALL_DIR/scripts/nginx-repo-manager.conf"
    conf_dst="/etc/nginx/conf.d/repo-manager.conf"
    if [[ -f "$conf_src" && -d /etc/nginx/conf.d ]]; then
        cp -a "$conf_dst" "$conf_dst.bak" 2>/dev/null || true
        cp "$conf_src" "$conf_dst"
        if out=$(nginx -t 2>&1); then
            if ! nginx -s reload >/dev/null 2>&1; then
                systemctl reload nginx >/dev/null 2>&1 || log "nginx не перечитал конфиг. Выполните: systemctl restart nginx"
            fi
        else
            if [[ -f "$conf_dst.bak" ]]; then
                cp -a "$conf_dst.bak" "$conf_dst"
            else
                rm -f "$conf_dst"
            fi
            log "новый конфиг nginx не применён: $out"
        fi
    fi
    systemctl start repo-manager
    log "обновление завершено, версия $(cat "$INSTALL_DIR/VERSION" 2>/dev/null || echo '?')"
    rm -rf "$tmp"
}

rollback() {
    if [[ ! -d "$BACKUP_DIR" ]]; then
        log "нет резервной копии $BACKUP_DIR"
        exit 1
    fi
    log "откат из $BACKUP_DIR"
    systemctl stop repo-manager || true
    sleep 1
    rm -rf "$INSTALL_DIR"
    mv "$BACKUP_DIR" "$INSTALL_DIR"
    systemctl start repo-manager
    log "откат завершён"
}

case "$ACTION" in
    apply) apply ;;
    rollback) rollback ;;
    install-helper) install_helper; log "helper установлен" ;;
    *) echo "usage: $0 apply <archive>|rollback|install-helper" >&2; exit 2 ;;
esac
