"""Проверки имён, URL, cron и путей архива. Без базы и aptly."""

import ipaddress
import re
import socket
from pathlib import Path, PurePosixPath
from urllib.parse import urljoin, urlparse

NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9,._+\- ]{0,199}$")


def fk_needs_rebuild(defn: str | None) -> bool:
    """Ключ пересоздаём только если его нет или он без ON DELETE CASCADE."""
    if not defn:
        return True
    return "ON DELETE CASCADE" not in defn.upper()


def name_error(name: str) -> str | None:
    if not NAME.match(name or ""):
        return "Имя: буквы, цифры, точка, _ и -, до 64 символов, без пробелов и слэшей"
    return None


def inside(path: Path, root: Path) -> bool:
    target = path.resolve()
    base = root.resolve()
    return target == base or base in target.parents


def host_blocked(host: str) -> bool:
    """Loopback и link-local. Частная сеть 10/8 и 192.168/16 остаётся разрешённой."""
    name = (host or "").strip("[]").rstrip(".").lower()
    if not name or name == "localhost" or name.startswith("localhost.") or name.endswith(".localhost"):
        return True
    try:
        addr = ipaddress.ip_address(name)
    except ValueError:
        return False
    return bool(addr.is_loopback or addr.is_link_local or addr.is_unspecified)


def _resolves_local(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        address = info[4][0]
        if host_blocked(address):
            return True
    return False


def url_error(url: str, resolve: bool = True) -> str | None:
    value = (url or "").strip()
    if not value.startswith(("http://", "https://")):
        return "URL только http или https"
    if any(char in value for char in " \n\r\t'\"<>"):
        return "URL содержит недопустимый символ"
    host = urlparse(value).hostname
    if not host:
        return "URL без узла"
    if host_blocked(host) or (resolve and _resolves_local(host)):
        return "URL указывает на локальный адрес"
    return None


def schedule_error(schedule: str | None) -> str | None:
    expr = (schedule or "").strip()
    if not expr:
        return None
    if len(expr.split()) != 5:
        return "Cron: 5 полей, например 0 3 * * *"
    try:
        from croniter import croniter
    except ImportError:
        return None
    try:
        if not croniter.is_valid(expr):
            return "Cron не разобран"
    except Exception:
        return "Cron не разобран"
    return None


def token_error(value: str | None, label: str) -> str | None:
    text = (value or "").strip()
    if not text:
        return None
    if not TOKEN.match(text):
        return f"{label}: только буквы, цифры, запятая, точка, пробел и _"
    return None


def mirror_field_error(payload: dict) -> str | None:
    if "source_url" in payload:
        err = url_error(str(payload.get("source_url") or ""))
        if err:
            return err
    if "schedule" in payload:
        err = schedule_error(payload.get("schedule"))
        if err:
            return err
    for key, label in (
        ("distribution", "Distribution"),
        ("components", "Components"),
        ("architectures", "Architectures"),
        ("repoid", "Repoid"),
    ):
        if key in payload:
            err = token_error(payload.get(key), label)
            if err:
                return err
    return None


def _show_field(text: str, label: str) -> str:
    prefix = label.lower() + ":"
    for line in (text or "").splitlines():
        if line.strip().lower().startswith(prefix):
            return line.split(":", 1)[1].strip()
    return ""


def _norm_url(url: str) -> str:
    return (url or "").strip().rstrip("/").lower()


def _norm_tokens(value: str) -> set[str]:
    return {item.strip().lower() for item in (value or "").replace(",", " ").split() if item.strip()}


def aptly_show_mismatch(show: str, url: str, distribution: str, components: str, architectures: str) -> str | None:
    """None, если существующее зеркало aptly можно подхватить. Иначе причина отказа."""
    if not (show or "").strip():
        return "aptly не показал параметры зеркала"
    got_url = _show_field(show, "Archive Root URL")
    if _norm_url(got_url) != _norm_url(url):
        return f"aptly уже хранит это имя с другим адресом: {got_url}"
    got_dist = _show_field(show, "Distribution")
    want_dist = (distribution or "stable").strip()
    if got_dist != want_dist:
        return f"aptly уже хранит это имя с дистрибутивом {got_dist}"
    got_comp = _norm_tokens(_show_field(show, "Components"))
    want_comp = _norm_tokens(components or "main")
    if got_comp != want_comp:
        return "aptly уже хранит это имя с другими компонентами: " + " ".join(sorted(got_comp))
    got_arch = _norm_tokens(_show_field(show, "Architectures"))
    want_arch = _norm_tokens(architectures or "amd64")
    if not want_arch <= got_arch:
        return "aptly уже хранит это имя с архитектурами: " + " ".join(sorted(got_arch))
    return None


def shown_package_count(on_disk: int | None, from_log: int) -> int:
    """На карточке во время sync видно уже лежащие пакеты, даже если лог ещё молчит."""
    logged = max(0, int(from_log or 0))
    if on_disk is None:
        return logged
    return max(int(on_disk), logged)


def deb_index_names(components: str, architectures: str) -> list[str]:
    """Стандартные пути Packages.gz. binary-amd64 уже включает пакеты all."""
    comps = [item.strip() for item in (components or "main").split(",") if item.strip()]
    archs = []
    for item in (architectures or "amd64").replace(",", " ").split():
        item = item.strip()
        if item and item != "all" and item not in archs:
            archs.append(item)
    if not archs:
        archs = ["amd64"]
    return [f"{comp}/binary-{arch}/Packages.gz" for comp in comps for arch in archs]


def indexes_from_release(text: str, components: str, architectures: str) -> list[str]:
    wanted = deb_index_names(components, architectures)
    found = []
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        path = parts[-1]
        if path in wanted and path not in found:
            found.append(path)
    return found or wanted


def sum_package_sizes(texts: list[str]) -> tuple[int, int]:
    """Число пакетов и сумма Size. Architecture all считается один раз."""
    seen_all = set()
    count = 0
    total = 0
    for text in texts:
        blocks = (text or "").replace("\r\n", "\n").split("\n\n")
        for block in blocks:
            fields = {}
            for line in block.splitlines():
                if not line or line[0] in " \t" or ":" not in line:
                    continue
                key, value = line.split(":", 1)
                fields[key.strip()] = value.strip()
            if "Package" not in fields or "Size" not in fields:
                continue
            try:
                size = int(fields["Size"])
            except ValueError:
                continue
            if fields.get("Architecture") == "all":
                key = (fields["Package"], fields.get("Version", ""))
                if key in seen_all:
                    continue
                seen_all.add(key)
            count += 1
            total += size
    return count, total


def bytes_still_needed(estimate: int | None, already: int | None) -> int | None:
    if not estimate:
        return None
    return max(0, int(estimate) - int(already or 0))


def space_block_message(free_bytes: int, need_bytes: int | None) -> str | None:
    """Не стартовать, если свободного места меньше, чем ещё предстоит скачать, плюс 1 ГБ."""
    if not need_bytes:
        return None
    margin = 1024 ** 3
    if free_bytes >= need_bytes + margin:
        return None
    need_gb = need_bytes / 1024 ** 3
    free_gb = free_bytes / 1024 ** 3
    return f"Нужно ещё около {need_gb:.1f} ГБ, свободно {free_gb:.1f} ГБ. Sync не запущен."


def publish_arch_flag(architectures: str | None) -> str:
    """Пакеты all не дают aptly угадать архитектуру, её надо передать явно."""
    parts = []
    for item in (architectures or "amd64").replace(",", " ").split():
        if item and item != "all" and item not in parts:
            parts.append(item)
    if not parts:
        parts = ["amd64"]
    return "-architectures=" + ",".join(parts)


_META_NAME = re.compile(r"repodata/[0-9a-f]{16,}-([A-Za-z0-9_.+-]+)")


def metadata_error(text: str) -> str:
    names = []
    for name in _META_NAME.findall(text or ""):
        if name not in names:
            names.append(name)
    if not names:
        return ""
    return "метаданные не скачаны: " + ", ".join(names[:6])


def tar_member_error(name: str) -> str | None:
    if not name or name.startswith("/") or name.startswith("\\"):
        return f"абсолютный путь: {name}"
    parts = PurePosixPath(name).parts
    if ".." in parts:
        return f"путь выходит наружу: {name}"
    if not parts or parts[0] != "repo-manager":
        return f"ожидается каталог repo-manager: {name}"
    return None


def http_status_and_location(raw: str) -> tuple[int | None, str | None]:
    """Последний блок заголовков curl -D -. Location сбрасывается на каждом HTTP/."""
    status = None
    location = None
    for line in (raw or "").splitlines():
        if line.startswith("HTTP/"):
            bits = line.split()
            status = int(bits[1]) if len(bits) > 1 and bits[1].isdigit() else None
            location = None
        elif line.lower().startswith("location:"):
            location = line.split(":", 1)[1].strip()
    return status, location


def next_hop(current: str, location: str) -> tuple[str | None, str | None]:
    """Следующий URL редиректа. Loopback и link-local не принимаются."""
    nxt = urljoin(current, location or "")
    err = url_error(nxt, resolve=False)
    if err:
        return None, err
    return nxt, None
