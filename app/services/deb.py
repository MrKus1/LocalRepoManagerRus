"""
Сервис для работы с deb-репозиториями через aptly.
На Oracle Linux aptly устанавливается как бинарник.
"""

import asyncio
import gzip
import io
import json
import logging
import shutil
import time
from pathlib import Path
from typing import Optional, Tuple

from app.config import settings
from app.guards import aptly_show_mismatch, indexes_from_release, publish_arch_flag, sum_package_sizes

logger = logging.getLogger(__name__)


async def run_cmd(cmd: list[str], cwd: Optional[str] = None) -> Tuple[int, str, str]:
    """Запуск команды асинхронно. stdbuf отдаёт строки сразу, чтобы счётчик не ждал конца процесса."""
    from app.services.procs import track, untrack, note
    from app.services.proxy import proxy_env
    env = proxy_env()
    env["PYTHONUNBUFFERED"] = "1"
    if cmd and cmd[0] != "stdbuf" and Path("/usr/bin/stdbuf").is_file():
        cmd = ["/usr/bin/stdbuf", "-oL", "-eL", *cmd]
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=cwd,
        env=env,
        start_new_session=True,
    )
    mirror_id = track(process)
    chunks = []
    try:
        assert process.stdout is not None
        while True:
            line = await process.stdout.readline()
            if not line:
                break
            text = line.decode(errors="replace")
            chunks.append(text)
            note(text)
        code = await process.wait()
        output = "".join(chunks)
        return code, output, ""
    finally:
        untrack(mirror_id)


def aptly_bin() -> str:
    configured = str(settings.APTLY_BIN or "aptly")
    if configured != "aptly" and Path(configured).is_file():
        return configured
    for candidate in ("/usr/local/bin/aptly", "/usr/bin/aptly"):
        if Path(candidate).is_file():
            return candidate
    return configured


_ESTIMATE_TTL = 12 * 3600
_estimate_lock = asyncio.Lock()


def _estimate_file() -> Path:
    return settings.STORAGE_ROOT / "tmp" / "estimates.json"


def _estimate_cache() -> dict:
    path = _estimate_file()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _estimate_key(url: str, distribution: str, components: str, architectures: str) -> str:
    return "|".join([
        (url or "").rstrip("/").lower(),
        (distribution or "stable").strip(),
        components or "main",
        architectures or "amd64",
    ])


def read_index_text(blob: bytes, limit: int = 200 * 1024 * 1024) -> str:
    """Читает Packages или Packages.gz, не разворачивая больше limit байт."""
    if blob[:2] == b"\x1f\x8b":
        chunks = []
        total = 0
        with gzip.GzipFile(fileobj=io.BytesIO(blob)) as handle:
            while True:
                piece = handle.read(1024 * 1024)
                if not piece:
                    break
                total += len(piece)
                if total > limit:
                    raise ValueError("индекс Packages больше 200 МБ")
                chunks.append(piece)
        return b"".join(chunks).decode("utf-8", "replace")
    if len(blob) > limit:
        raise ValueError("индекс Packages больше 200 МБ")
    return blob.decode("utf-8", "replace")


class DebService:
    def __init__(self):
        self.aptly = aptly_bin()

    async def create_mirror(
        self,
        name: str,
        url: str,
        distribution: str,
        components: str = "main",
        architectures: str = "amd64",
    ) -> Tuple[bool, str]:
        """
        Создать зеркало:
        aptly mirror create -architectures=amd64 name url distribution component1 component2
        """
        cmd = [
            self.aptly, "mirror", "create",
            f"-architectures={architectures}",
            name,
            url,
            distribution,
        ]
        # components добавляются как отдельные аргументы
        cmd.extend(components.split(","))

        try:
            names = await self.list_mirrors()
        except RuntimeError as exc:
            return False, str(exc)
        if name in names:
            code, out, err = await run_cmd([self.aptly, "mirror", "show", name])
            text = out or err
            if code != 0:
                return False, (text or "aptly mirror show")[:800]
            reason = aptly_show_mismatch(text, url, distribution, components, architectures)
            if reason:
                return False, reason
            return True, "подхвачено существующее зеркало aptly"

        code, out, err = await run_cmd(cmd)
        if code != 0:
            logger.error(f"Failed to create mirror {name}: {err}")
            return False, err or out
        return True, out

    async def reconfigure(
        self,
        name: str,
        url: str,
        distribution: str,
        components: str,
        architectures: str,
        old_distribution: str,
        old_components: str,
    ) -> Tuple[bool, str]:
        """Применяет URL и архитектуру в aptly. Смена дистрибутива или компонентов создаёт зеркало заново."""
        architectures = (architectures or "amd64").replace(" ", "")
        components = components or "main"
        distribution = distribution or "stable"
        exists = name in await self.list_mirrors()
        if not exists:
            return await self.create_mirror(name, url, distribution, components, architectures)

        if distribution != (old_distribution or "stable") or components != (old_components or "main"):
            code, out, err = await run_cmd([self.aptly, "mirror", "drop", "-force", name])
            if code != 0:
                return False, err or out
            return await self.create_mirror(name, url, distribution, components, architectures)

        code, out, err = await run_cmd([
            self.aptly, "mirror", "edit",
            f"-archive-url={url}",
            f"-architectures={architectures}",
            name,
        ])
        if code != 0:
            return False, err or out
        return True, out

    async def update_mirror(self, name: str) -> Tuple[bool, str]:
        """aptly mirror update name"""
        cmd = [self.aptly, "mirror", "update", name]
        code, out, err = await run_cmd(cmd)
        if code != 0:
            return False, err or out
        return True, out

    async def publish_mirror(self, name: str, distribution: str, architectures: str = "amd64") -> Tuple[bool, str]:
        """Снимок и публикация в filesystem:default. Каталог префикса создаёт сам aptly."""
        from datetime import datetime
        public = settings.STORAGE_ROOT / "public"
        public.mkdir(parents=True, exist_ok=True)
        snapshot = f"{name}-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
        ok, msg = await self.create_snapshot(name, snapshot)
        if not ok:
            return False, msg

        prefix = f"filesystem:default:{name}"
        arch = publish_arch_flag(architectures)
        try:
            published = any(
                pub_name == name and dist == distribution
                for _prefix, dist, pub_name in await self.list_publishes()
            )
        except RuntimeError as exc:
            return False, str(exc)

        target = public / name
        release = target / "dists" / distribution / "Release"
        if not published and target.is_dir() and not release.is_file():
            shutil.rmtree(target)

        if published:
            code, out, err = await run_cmd([
                self.aptly, "publish", "switch", arch, distribution, prefix, snapshot,
            ])
            if code != 0:
                return False, err or out
            return True, out

        code, out, err = await run_cmd([
            self.aptly, "publish", "snapshot",
            arch,
            f"-distribution={distribution}",
            snapshot,
            prefix,
        ])
        text = err or out or ""
        if code == 0:
            return True, out
        if "already" in text.lower():
            code2, out2, err2 = await run_cmd([
                self.aptly, "publish", "switch", arch, distribution, prefix, snapshot,
            ])
            if code2 != 0:
                return False, err2 or out2 or text
            return True, out2
        return False, text or f"publish snapshot: {code}"

    async def create_snapshot(self, mirror_name: str, snapshot_name: str) -> Tuple[bool, str]:
        cmd = [self.aptly, "snapshot", "create", snapshot_name, "from", "mirror", mirror_name]
        code, out, err = await run_cmd(cmd)
        return code == 0, err or out

    async def publish_snapshot(
        self,
        snapshot_name: str,
        distribution: str,
        prefix: str = ".",
    ) -> Tuple[bool, str]:
        """
        aptly publish snapshot -distribution=jammy snapshot_name prefix
        """
        cmd = [
            self.aptly, "publish", "snapshot",
            f"-distribution={distribution}",
            snapshot_name,
            prefix,
        ]
        code, out, err = await run_cmd(cmd)
        return code == 0, err or out

    async def create_local_repo(self, name: str) -> Tuple[bool, str]:
        """Создать локальный репозиторий для своих пакетов"""
        cmd = [self.aptly, "repo", "create", name]
        code, out, err = await run_cmd(cmd)
        return code == 0, err or out

    async def add_package_to_repo(self, repo_name: str, package_path: str) -> Tuple[bool, str]:
        """Добавить .deb в локальный репозиторий"""
        cmd = [self.aptly, "repo", "add", repo_name, package_path]
        code, out, err = await run_cmd(cmd)
        return code == 0, err or out

    async def publish_repo(
        self,
        repo_name: str,
        distribution: str,
        component: str = "main",
        architectures: str = "amd64",
        prefix: str = ".",
    ) -> Tuple[bool, str]:
        cmd = [
            self.aptly, "publish", "repo",
            f"-distribution={distribution}",
            f"-component={component}",
            f"-architectures={architectures}",
            repo_name,
            prefix,
        ]
        code, out, err = await run_cmd(cmd)
        return code == 0, err or out

    async def estimate(self, url: str, distribution: str, components: str, architectures: str) -> dict:
        """Сколько пакетов и байт в индексе. Кэш 12 часов, без повторной закачки при каждом открытии страницы."""
        key = _estimate_key(url, distribution, components, architectures)
        cached = _estimate_cache().get(key) or {}
        if cached and time.time() - float(cached.get("at") or 0) < _ESTIMATE_TTL and not cached.get("error"):
            return {"packages": cached.get("packages"), "bytes": cached.get("bytes"), "error": ""}
        async with _estimate_lock:
            cached = _estimate_cache().get(key) or {}
            if cached and time.time() - float(cached.get("at") or 0) < _ESTIMATE_TTL and not cached.get("error"):
                return {"packages": cached.get("packages"), "bytes": cached.get("bytes"), "error": ""}
            if cached.get("error") and time.time() - float(cached.get("at") or 0) < 600:
                return {"packages": None, "bytes": None, "error": cached.get("error")}
            payload = await self._fetch_estimate(url, distribution, components, architectures)
            store = _estimate_cache()
            store[key] = {**payload, "at": time.time()}
            path = _estimate_file()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(store))
            return payload

    async def _fetch_estimate(self, url: str, distribution: str, components: str, architectures: str) -> dict:
        from app.services.rpm import download_checked

        root = (url or "").rstrip("/")
        dist = distribution or "stable"
        dest = settings.STORAGE_ROOT / "tmp" / "estimate-index"
        dest.parent.mkdir(parents=True, exist_ok=True)
        release_url = f"{root}/dists/{dist}/Release"
        final, err = await download_checked(release_url, dest)
        if err or not dest.exists():
            dest.unlink(missing_ok=True)
            return {"packages": None, "bytes": None, "error": err or "Release недоступен"}
        release = dest.read_text(errors="replace")
        dest.unlink(missing_ok=True)
        texts = []
        for name in indexes_from_release(release, components, architectures):
            final, err = await download_checked(f"{root}/dists/{dist}/{name}", dest)
            if err or not dest.exists():
                dest.unlink(missing_ok=True)
                continue
            try:
                texts.append(read_index_text(dest.read_bytes()))
            except (OSError, ValueError, gzip.BadGzipFile) as exc:
                dest.unlink(missing_ok=True)
                return {"packages": None, "bytes": None, "error": str(exc)[:200]}
            dest.unlink(missing_ok=True)
        if not texts:
            return {"packages": None, "bytes": None, "error": "индекс Packages не скачался"}
        count, total = sum_package_sizes(texts)
        return {"packages": count, "bytes": total, "error": ""}

    async def run_check(self, cmd: list[str]) -> Tuple[int, str, str]:
        return await run_cmd(cmd)

    async def recover(self) -> Tuple[bool, str]:
        code, out, err = await run_cmd([self.aptly, "db", "recover"])
        text = (err or out or "").strip()
        return code == 0 and not _db_broken(text), text

    async def _listed(self, args: list[str]) -> Tuple[int, str]:
        code, out, err = await run_cmd([self.aptly, *args])
        text = err or out
        if code != 0 and _db_broken(text):
            ok, msg = await self.recover()
            if not ok:
                return 1, "База aptly повреждена, восстановление не удалось: " + (msg or text)[:800]
            code, out, err = await run_cmd([self.aptly, *args])
            text = err or out
        return code, text

    async def cleanup(self) -> Tuple[bool, str]:
        code, text = await self._listed(["db", "cleanup"])
        return code == 0, text

    async def list_mirrors(self) -> list[str]:
        code, text = await self._listed(["mirror", "list", "-raw"])
        if code != 0:
            raise RuntimeError((text or "aptly mirror list")[:800])
        return [line.strip() for line in text.splitlines() if line.strip()]

    async def list_publishes(self) -> list[tuple[str, str, str]]:
        """prefix, distribution, короткое имя публикации."""
        code, text = await self._listed(["publish", "list", "-raw"])
        if code != 0:
            raise RuntimeError((text or "aptly publish list")[:800])
        rows = []
        for line in text.splitlines():
            parts = line.split()
            if len(parts) < 2:
                continue
            prefix, distribution = parts[0], parts[-1]
            name = prefix.rstrip("/").split(":")[-1]
            rows.append((prefix, distribution, name))
        return rows

    async def forget_mirror(self, name: str, distribution: str = "") -> list[str]:
        """Снимает снимки, публикацию и зеркало aptly. Пул не чистит."""
        notes = []
        code, out, err = await run_cmd([self.aptly, "snapshot", "list", "-raw"])
        snapshots = [line.strip() for line in out.splitlines() if line.strip()] if code == 0 else []
        for snapshot in snapshots:
            if snapshot == name or snapshot.startswith(f"{name}-"):
                c, o, e = await run_cmd([self.aptly, "snapshot", "drop", "-force", snapshot])
                notes.append(e or o or f"snapshot {snapshot}: {c}")
        seen = set()
        for prefix, dist, pub_name in await self.list_publishes():
            if pub_name != name:
                continue
            seen.add((prefix, dist))
            c, o, e = await run_cmd([self.aptly, "publish", "drop", dist, prefix])
            notes.append(e or o or f"publish {prefix} {dist}: {c}")
        guessed = (distribution or "").strip()
        prefix = f"filesystem:default:{name}"
        if guessed and (prefix, guessed) not in seen:
            c, o, e = await run_cmd([self.aptly, "publish", "drop", guessed, prefix])
            notes.append(e or o or f"publish {prefix} {guessed}: {c}")
        c, o, e = await run_cmd([self.aptly, "mirror", "drop", "-force", name])
        text = (e or o or "").strip()
        notes.append(("mirror: %s %s" % (c, text)).strip())
        return notes

    async def drop_mirror(self, name: str, distribution: str = "stable", cleanup: bool = True) -> Tuple[bool, str]:
        """Удаляет только это зеркало и его снимки. Чужие зеркала не трогает."""
        notes = await self.forget_mirror(name, distribution)
        text = "\n".join(notes)
        ok = mirror_drop_ok(notes)
        if not cleanup:
            return ok, text
        c, o, e = await run_cmd([self.aptly, "db", "cleanup"])
        notes.append(e or o or f"cleanup: {c}")
        return ok and c == 0, "\n".join(notes)

    async def drop_orphans(self, keep: set[str]) -> Tuple[list[str], str]:
        """Снимает публикации и каталоги, которых уже нет в базе."""
        try:
            names = {name for name in await self.list_mirrors() if name not in keep}
            for _prefix, _dist, name in await self.list_publishes():
                if name not in keep:
                    names.add(name)
        except RuntimeError as exc:
            return [], str(exc)
        roots = [
            settings.STORAGE_ROOT / "public",
            settings.MIRRORS_DIR / "deb",
            settings.MIRRORS_DIR / "rpm",
        ]
        for root in roots:
            if not root.is_dir():
                continue
            for child in root.iterdir():
                if child.is_dir() and child.name not in keep:
                    names.add(child.name)
        removed = []
        errors = []
        for name in sorted(names):
            if not _orphan_name(name) or name in keep:
                continue
            try:
                await self.forget_mirror(name)
            except Exception as exc:
                errors.append(f"{name}: {exc}")
            for root in roots:
                path = root / name
                if not path.is_dir():
                    continue
                target = path.resolve()
                base = root.resolve()
                if base not in target.parents:
                    errors.append(f"пропуск {target}")
                    continue
                shutil.rmtree(target)
            removed.append(name)
        return removed, "\n".join(errors)


def mirror_drop_ok(notes: list[str]) -> bool:
    """True, если aptly снял зеркало или его уже не было."""
    for line in notes:
        if not line.startswith("mirror:"):
            continue
        if line.startswith("mirror: 0"):
            return True
        low = line.lower()
        if "not found" in low or "не найден" in low:
            return True
    return False


def _db_broken(text: str) -> bool:
    low = (text or "").lower()
    return "leveldb" in low or "manifest corrupted" in low


def _orphan_name(name: str) -> bool:
    if not name or name in (".", "..", "dists", "pool"):
        return False
    return "/" not in name and "\\" not in name and ".." not in name


deb_service = DebService()
