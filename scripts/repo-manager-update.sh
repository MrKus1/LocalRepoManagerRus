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
