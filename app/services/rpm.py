"""
Сервис для работы с RPM-репозиториями.
На Oracle Linux 10 reposync — плагин dnf: `dnf reposync`.
"""

import asyncio
import logging
import shutil
from pathlib import Path
from typing import Optional, Tuple

from app.config import settings

logger = logging.getLogger(__name__)


async def run_cmd(cmd: list[str], cwd: Optional[str] = None) -> Tuple[int, str, str]:
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


class RpmService:
    def __init__(self):
        self.createrepo = settings.CREATEREPO_BIN or "/usr/bin/createrepo_c"

    async def sync_mirror(
        self,
        dest_path: str,
        repoid: Optional[str] = None,
        source_url: Optional[str] = None,
        newest_only: bool = True,
        delete: bool = False,
    ) -> Tuple[bool, str]:
        dest = Path(dest_path)
        dest.mkdir(parents=True, exist_ok=True)

        cmd = [
            "dnf",
            "--exclude=*.src.rpm",
            "reposync",
            "--download-metadata",
            "-p", str(dest.parent),
        ]
        if newest_only:
            cmd.append("--newest-only")
        if delete:
            cmd.append("--delete")

        if repoid:
            cmd.extend(["--repoid", repoid])
        if source_url:
            url = source_url.rstrip("/") + "/repodata/repomd.xml"
            code, out, err = await run_cmd(["curl", "-fsSL", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "20", url])
            if code != 0 or out.strip() != "200":
                return False, f"repomd.xml недоступен: {url} http={out.strip() or 'нет'} {err.strip()[:200]}"
            temp_name = dest.name
            cmd.extend([
                f"--repofrompath={temp_name},{source_url}",
                "--repo", temp_name,
            ])
        else:
            return False, "Нужен либо repoid, либо source_url"

        code, out, err = await run_cmd(cmd)
        if code != 0:
            logger.error("reposync failed: %s", err)
            return False, err or out

        meta_path = dest if (dest / "repodata").exists() or any(dest.glob("*.rpm")) else dest
        # reposync кладёт пакеты в dest.parent / repo_name
        await self.update_metadata(str(dest))
        return True, out

    async def update_metadata(self, repo_path: str) -> Tuple[bool, str]:
        path = Path(repo_path)
        path.mkdir(parents=True, exist_ok=True)
        if (path / "repodata").exists():
            cmd = [self.createrepo, "--update", str(path)]
        else:
            cmd = [self.createrepo, str(path)]
        code, out, err = await run_cmd(cmd)
        if code != 0:
            return False, err or out
        return True, out

    async def create_local_repo(self, path: str) -> Tuple[bool, str]:
        Path(path).mkdir(parents=True, exist_ok=True)
        return await self.update_metadata(path)

    async def add_package(self, repo_path: str, package_path: str) -> Tuple[bool, str]:
        src = Path(package_path)
        dest_dir = Path(repo_path)
        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest_dir / src.name)
        return await self.update_metadata(str(dest_dir))

    async def keep_newest(self, repo_path: str) -> Tuple[int, str]:
        """Оставляет самый новый RPM каждого имени и пересобирает repodata."""
        root = Path(repo_path)
        rpms = [p for p in root.rglob("*.rpm") if p.is_file()]
        newest: dict[str, tuple] = {}
        for path in rpms:
            code, out, err = await run_cmd([
                "rpm", "-qp", "--qf", "%{NAME}|%{EPOCH}|%{VERSION}|%{RELEASE}", str(path)
            ])
            if code != 0:
                continue
            name, epoch, version, release = (out.strip().split("|") + ["0", "", ""])[:4]
            epoch = "0" if epoch in ("(none)", "") else epoch
            key = tuple(int(x) if x.isdigit() else x for x in (epoch, version, release))
            current = newest.get(name)
            if current is None or key > current[0]:
                newest[name] = (key, path)
        keep = {item[1] for item in newest.values()}
        removed = 0
        for path in rpms:
            if path not in keep:
                path.unlink()
                removed += 1
        if removed:
            ok, msg = await self.update_metadata(str(root))
            if not ok:
                return removed, msg
        return removed, ""


rpm_service = RpmService()
