import json
import os
from pathlib import Path

from app.config import settings

SETTINGS_FILE = Path("/var/lib/repo-manager/proxy.json")


def load_proxy() -> dict:
    data = {"proxy_url": settings.PROXY_URL or "", "no_proxy": settings.NO_PROXY or "127.0.0.1,localhost"}
    if SETTINGS_FILE.exists():
        try:
            saved = json.loads(SETTINGS_FILE.read_text())
            data.update({k: saved.get(k, data[k]) for k in data})
        except (OSError, json.JSONDecodeError):
            pass
    return data


def save_proxy(proxy_url: str, no_proxy: str) -> dict:
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {"proxy_url": proxy_url.strip(), "no_proxy": no_proxy.strip() or "127.0.0.1,localhost"}
    SETTINGS_FILE.write_text(json.dumps(data))
    return data


def proxy_env() -> dict:
    env = os.environ.copy()
    data = load_proxy()
    proxy = data.get("proxy_url") or ""
    if proxy:
        env["http_proxy"] = proxy
        env["https_proxy"] = proxy
        env["HTTP_PROXY"] = proxy
        env["HTTPS_PROXY"] = proxy
    else:
        for key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
            env.pop(key, None)
    env["no_proxy"] = data.get("no_proxy") or "127.0.0.1,localhost"
    env["NO_PROXY"] = env["no_proxy"]
    return env
