Name:           repo-manager
Version:        1.0.0
Release:        1%{?dist}
Summary:        Local deb/rpm mirror manager
License:        MIT
BuildArch:      x86_64
Requires:       python3, python3-pip, createrepo_c, dnf-plugins-core, postgresql-server, nginx

%description
Offline package. Python dependencies are installed from bundled wheels.
Does not download anything during install.

%install
rm -rf %{buildroot}
mkdir -p %{buildroot}/opt/repo-manager
cp -a %{_sourcedir}/app %{buildroot}/opt/repo-manager/
cp -a %{_sourcedir}/wheels %{buildroot}/opt/repo-manager/
cp -a %{_sourcedir}/scripts %{buildroot}/opt/repo-manager/
mkdir -p %{buildroot}/opt/repo-manager/bin
cp -a %{_sourcedir}/bin/aptly %{buildroot}/opt/repo-manager/bin/ 2>/dev/null || true
cp -a %{_sourcedir}/requirements.txt %{buildroot}/opt/repo-manager/
cp -a %{_sourcedir}/.env.example %{buildroot}/opt/repo-manager/ 2>/dev/null || true

%files
/opt/repo-manager

%post
set -e
id repoman &>/dev/null || useradd -r -s /sbin/nologin -d /var/lib/repo-manager repoman
mkdir -p /var/lib/repo-manager/{mirrors,local,uploads,logs,public,aptly}
if [ ! -f /opt/repo-manager/.env ]; then
  cat > /opt/repo-manager/.env << 'EOF'
POSTGRES_USER=repoman
POSTGRES_PASSWORD=changeme
POSTGRES_HOST=127.0.0.1
POSTGRES_PORT=5432
POSTGRES_DB=repo_manager
DEBUG=false
EOF
fi
python3 -m venv /opt/repo-manager/venv
/opt/repo-manager/venv/bin/pip install --no-index --find-links=/opt/repo-manager/wheels -r /opt/repo-manager/requirements.txt
install -m 0755 /opt/repo-manager/bin/aptly /usr/local/bin/aptly 2>/dev/null || true
if [ ! -f /etc/aptly.conf ]; then
  cat > /etc/aptly.conf << 'EOF'
{
  "rootDir": "/var/lib/repo-manager/aptly",
  "gpgDisableSign": true,
  "gpgDisableVerify": true,
  "downloadSourcePackages": false,
  "FileSystemPublishEndpoints": {
    "default": {"rootDir": "/var/lib/repo-manager/public", "linkMethod": "hardlink"}
  }
}
EOF
fi
cat > /etc/systemd/system/repo-manager.service << 'EOF'
[Unit]
Description=Repo Manager
After=network.target postgresql.service

[Service]
User=repoman
Group=repoman
WorkingDirectory=/opt/repo-manager
Environment=PATH=/opt/repo-manager/venv/bin:/usr/local/bin:/usr/bin
EnvironmentFile=/opt/repo-manager/.env
ExecStart=/opt/repo-manager/venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
chown -R repoman:repoman /var/lib/repo-manager /opt/repo-manager/venv /opt/repo-manager/.env

if ! systemctl is-active --quiet postgresql 2>/dev/null; then
  postgresql-setup --initdb || true
  systemctl enable --now postgresql || true
fi
PG_PASS=$(grep ^POSTGRES_PASSWORD= /opt/repo-manager/.env | cut -d= -f2-)
sudo -u postgres psql -tc "SELECT 1 FROM pg_roles WHERE rolname='repoman'" | grep -q 1 || \
  sudo -u postgres psql -c "CREATE USER repoman WITH PASSWORD '${PG_PASS}';"
sudo -u postgres psql -tc "SELECT 1 FROM pg_database WHERE datname='repo_manager'" | grep -q 1 || \
  sudo -u postgres psql -c "CREATE DATABASE repo_manager OWNER repoman;"
sudo -u postgres psql -d repo_manager -c "GRANT ALL ON SCHEMA public TO repoman;" || true
if [ -f /var/lib/pgsql/data/pg_hba.conf ]; then
  sed -i -E 's/^(local|host)\s+all\s+all\s+.*(ident|peer)\s*$/\1 all all 127.0.0.1\/32 scram-sha-256/' /var/lib/pgsql/data/pg_hba.conf || true
  systemctl reload postgresql || systemctl restart postgresql || true
fi

if [ -f /opt/repo-manager/scripts/nginx-repo-manager.conf ]; then
  cp /opt/repo-manager/scripts/nginx-repo-manager.conf /etc/nginx/conf.d/repo-manager.conf
  nginx -t && systemctl enable --now nginx && systemctl reload nginx || true
fi

systemctl daemon-reload
systemctl enable repo-manager
echo "Проверь пароль в /opt/repo-manager/.env и systemctl start repo-manager"

%postun
systemctl disable --now repo-manager >/dev/null 2>&1 || true
