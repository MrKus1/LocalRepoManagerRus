#!/bin/bash
# Сборка RPM на Oracle Linux. Интернет не нужен, если уже есть rpm-build.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
dnf install -y rpm-build
WORKDIR="$(mktemp -d)"
mkdir -p "$WORKDIR"/{BUILD,RPMS,SOURCES,SPECS,SRPMS}
cp "$ROOT/packaging/repo-manager.spec" "$WORKDIR/SPECS/"
# исходники кладём как SOURCES, spec копирует их в buildroot
ln -s "$ROOT/app" "$WORKDIR/SOURCES/app"
ln -s "$ROOT/wheels" "$WORKDIR/SOURCES/wheels"
ln -s "$ROOT/scripts" "$WORKDIR/SOURCES/scripts"
ln -s "$ROOT/bin" "$WORKDIR/SOURCES/bin"
ln -s "$ROOT/requirements.txt" "$WORKDIR/SOURCES/requirements.txt"
ln -s "$ROOT/.env.example" "$WORKDIR/SOURCES/.env.example" 2>/dev/null || true
rpmbuild --define "_topdir $WORKDIR" --define "_sourcedir $ROOT" -bb "$ROOT/packaging/repo-manager.spec"
find "$WORKDIR/RPMS" -name '*.rpm' -exec cp {} "$ROOT/../" \;
echo "RPM: $ROOT/../"
find "$ROOT/../" -name 'repo-manager-*.rpm' -printf '%p %s\n'
