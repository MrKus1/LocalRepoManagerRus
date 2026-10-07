#!/bin/bash
# Сборка офлайн-дистрибутива
# Запускать на машине С интернетом (можно не OL10)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
WHEELS_DIR="$ROOT_DIR/wheels"
BIN_DIR="$ROOT_DIR/bin"
APTLY_VERSION="${APTLY_VERSION:-1.5.0}"
ARCH="${ARCH:-amd64}"   # amd64 или arm64

echo "==> Очистка старых wheels"
rm -rf "$WHEELS_DIR"
mkdir -p "$WHEELS_DIR" "$BIN_DIR"

echo "==> Скачивание Python-зависимостей (wheels)"
pip3 download \
    -r "$ROOT_DIR/requirements.txt" \
    -d "$WHEELS_DIR" \
    --python-version 3.12 \
    --only-binary=:all: \
    2>/dev/null || \
pip3 download \
    -r "$ROOT_DIR/requirements.txt" \
    -d "$WHEELS_DIR"

# greenlet часто нужен отдельно
pip3 download greenlet -d "$WHEELS_DIR" --python-version 3.12 2>/dev/null || true

echo "==> Скачивание aptly ${APTLY_VERSION} (${ARCH})"
APTLY_URL="https://github.com/aptly-dev/aptly/releases/download/v${APTLY_VERSION}/aptly_${APTLY_VERSION}_linux_${ARCH}.tar.gz"
TMP=$(mktemp -d)
curl -fsSL -o "$TMP/aptly.tgz" "$APTLY_URL"
tar -xzf "$TMP/aptly.tgz" -C "$TMP"
# Архив может содержать папку или сразу бинарник
find "$TMP" -type f -name aptly -exec cp {} "$BIN_DIR/aptly" \;
chmod +x "$BIN_DIR/aptly"
rm -rf "$TMP"

echo "==> Проверка"
ls -lh "$BIN_DIR/aptly"
echo "Wheels: $(ls "$WHEELS_DIR" | wc -l) файлов"
du -sh "$WHEELS_DIR"

echo ""
echo "Готово. Теперь упакуй весь каталог repo-manager:"
echo "  tar czf repo-manager-offline.tar.gz -C $(dirname "$ROOT_DIR") $(basename "$ROOT_DIR")"
echo ""
echo "На целевом сервере (без интернета):"
echo "  tar xzf repo-manager-offline.tar.gz"
echo "  cd repo-manager"
echo "  sudo ./scripts/install-offline.sh"
