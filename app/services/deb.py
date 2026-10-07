"""
Сервис для работы с deb-репозиториями через aptly.
На Oracle Linux aptly устанавливается как бинарник.
"""

import asyncio
import logging
from pathlib import Path
from typing import Optional, Tuple

from app.config import settings

logger = logging.getLogger(__name__)


async def run_cmd(cmd: list[str], cwd: Optional[str] = None) -> Tuple[int, str, str]:
    """Запуск команды асинхронно"""
    from app.services.procs import track, untrack, note
    from app.services.proxy import proxy_env
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=cwd,
        env=proxy_env(),
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


class DebService:
    def __init__(self):
        self.aptly = settings.APTLY_BIN

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

        code, out, err = await run_cmd(cmd)
        if code != 0:
            logger.error(f"Failed to create mirror {name}: {err}")
            return False, err or out
        return True, out

    async def update_mirror(self, name: str) -> Tuple[bool, str]:
        """aptly mirror update name"""
        cmd = [self.aptly, "mirror", "update", name]
        code, out, err = await run_cmd(cmd)
        if code != 0:
            return False, err or out
        return True, out

    async def publish_mirror(self, name: str, distribution: str) -> Tuple[bool, str]:
        """Снимок и публикация в filesystem:default (/var/lib/repo-manager/public)."""
        from datetime import datetime
        settings.STORAGE_ROOT.joinpath("public").mkdir(parents=True, exist_ok=True)
        snapshot = f"{name}-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
        ok, msg = await self.create_snapshot(name, snapshot)
        if not ok:
            return False, msg

        prefix = f"filesystem:default:{name}"
        (settings.STORAGE_ROOT / "public" / name).mkdir(parents=True, exist_ok=True)
        code, out, err = await run_cmd([
            self.aptly, "publish", "snapshot",
            f"-distribution={distribution}",
            snapshot,
            prefix,
        ])
        if code == 0:
            return True, out

        # уже опубликовано — переключаем на новый снимок
        code2, out2, err2 = await run_cmd([
            self.aptly, "publish", "switch",
            distribution,
            prefix,
            snapshot,
        ])
        if code2 != 0:
            return False, (err2 or out2 or err or out)
        return True, out2

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

    async def run_check(self, cmd: list[str]) -> Tuple[int, str, str]:
        return await run_cmd(cmd)

    async def cleanup(self) -> Tuple[bool, str]:
        code, out, err = await run_cmd([self.aptly, "db", "cleanup"])
        return code == 0, err or out

    async def list_mirrors(self) -> list[str]:
        code, out, err = await run_cmd([self.aptly, "mirror", "list", "-raw"])
        if code != 0:
            return []
        return [line.strip() for line in out.splitlines() if line.strip()]

    async def drop_mirror(self, name: str, distribution: str = "stable") -> Tuple[bool, str]:
        """Удаляет только это зеркало и его снимки. Чужие зеркала не трогает."""
        messages = []
        code, out, err = await run_cmd([self.aptly, "snapshot", "list", "-raw"])
        snapshots = [line.strip() for line in out.splitlines() if line.strip()] if code == 0 else []
        for snapshot in snapshots:
            if snapshot == name or snapshot.startswith(f"{name}-"):
                c, o, e = await run_cmd([self.aptly, "snapshot", "drop", "-force", snapshot])
                messages.append(e or o or f"snapshot {snapshot}: {c}")

        c, o, e = await run_cmd([self.aptly, "publish", "drop", distribution, f"filesystem:default:{name}"])
        messages.append(e or o or f"publish: {c}")

        c, o, e = await run_cmd([self.aptly, "mirror", "drop", "-force", name])
        messages.append(e or o or f"mirror: {c}")

        c, o, e = await run_cmd([self.aptly, "db", "cleanup"])
        messages.append(e or o or f"cleanup: {c}")
        return c == 0, "\n".join(messages)


deb_service = DebService()
