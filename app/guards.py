"""Проверки имён, URL, cron и путей архива. Без базы и aptly."""

import re
from pathlib import Path, PurePosixPath

NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9,._+\- ]{0,199}$")


def name_error(name: str) -> str | None:
    if not NAME.match(name or ""):
        return "Имя: буквы, цифры, точка, _ и -, до 64 символов, без пробелов и слэшей"
    return None


def inside(path: Path, root: Path) -> bool:
    target = path.resolve()
    base = root.resolve()
    return target == base or base in target.parents


def url_error(url: str) -> str | None:
    value = (url or "").strip()
    if not value.startswith(("http://", "https://")):
        return "URL только http или https"
    if any(char in value for char in " \n\r\t'\"<>"):
        return "URL содержит недопустимый символ"
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
