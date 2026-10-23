"""
Сервис для работы с RPM-репозиториями.
На Oracle Linux 10 reposync — плагин dnf: `dnf reposync`.
"""

import asyncio
import logging
import re
import shutil
from pathlib import Path
from typing import Optional, Tuple

from app.config import settings
from app.guards import metadata_error

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

        source = (source_url or "").rstrip("/") + "/" if source_url else ""
        cmd = [
            "dnf",
            "--exclude=*.src.rpm",
            "--disablerepo=*",
            "--nogpgcheck",
            "--setopt=fastestmirror=0",
            "--setopt=retries=8",
            "--setopt=timeout=180",
            "--setopt=max_parallel_downloads=2",
            "reposync",
            "-p", str(dest.parent),
        ]
        if newest_only:
            cmd.append("--newest-only")
        if delete:
            cmd.append("--delete")

        if repoid and not source_url:
            cmd.extend(["--repoid", repoid])
        if source_url:
            url = source + "repodata/repomd.xml"
            code, out, err = await run_cmd(["curl", "-fsSL", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "30", "-L", url])
            if code != 0 or out.strip() != "200":
                return False, f"repomd.xml недоступен: {url} http={out.strip() or 'нет'} {err.strip()[:200]}"
            temp_name = dest.name
            cmd.extend([
                f"--repofrompath={temp_name},{source}",
                "--enablerepo", temp_name,
                f"--setopt={temp_name}.metadata_expire=0",
            ])
        else:
            return False, "Нужен либо repoid, либо source_url"

        code, out, err = await run_cmd(cmd)
        if code != 0:
            text = (err or out or "").strip()
            short = metadata_error(text)
            if not short:
                short = next((line for line in reversed(text.splitlines()) if "RuntimeError:" in line or "Error:" in line or "Cannot download" in line), "")
            if not short:
                short = text.splitlines()[-1] if text else "reposync завершился с ошибкой"
            logger.error("reposync failed: %s", short)
            return False, short[:500]

        meta_note = await self.fetch_upstream_metadata(source, dest)
        ok_meta, meta = await self.update_metadata(str(dest))
        if not ok_meta:
            return False, (meta or "createrepo_c не собрал repodata")[:500]
        return True, ((out or "") + ("\n" + meta_note if meta_note else "")).strip()

    async def fetch_upstream_metadata(self, source: str, dest: Path) -> str:
        """Кладёт comps/modules/updateinfo рядом с пакетами. Битый sqlite не валит sync."""
        if not source:
            return ""
        import tempfile
        failed = []
        saved = []
        with tempfile.NamedTemporaryFile(prefix="repomd-", suffix=".xml") as tmp:
            code, _out, err = await run_cmd([
                "curl", "-fsSL", "--retry", "3", "--max-time", "60", "-o", tmp.name, source + "repodata/repomd.xml",
            ])
            if code != 0:
                return "repomd.xml не скачан: " + (err or "")[:160]
            text = Path(tmp.name).read_text(errors="replace")
        hrefs = re.findall(r'href="(repodata/[^"]+)"', text)
        meta_dir = dest / "repodata"
        meta_dir.mkdir(parents=True, exist_ok=True)
        for href in hrefs:
            if "sqlite" in href:
                continue
            filename = href.rsplit("/", 1)[-1]
            target = meta_dir / filename
            code, _out, _err = await run_cmd([
                "curl", "-fL", "--retry", "3", "--retry-delay", "2", "--max-time", "180",
                "-o", str(target), source + href,
            ])
            if code != 0:
                target.unlink(missing_ok=True)
                failed.append(filename.split("-", 1)[-1])
            else:
                saved.append(filename)
        if not any("primary" in name for name in saved):
            return "primary.xml.gz не скачан" + (": " + ", ".join(failed[:4]) if failed else "")
        saved_set = set(saved)

        def keep_block(match):
            block = match.group(0)
            names = re.findall(r'href="repodata/([^"]+)"', block)
            if names and all(name in saved_set for name in names):
                return block
            return ""

        filtered = re.sub(r'<data type="[^"]+">.*?</data>', keep_block, text, flags=re.S)
        (meta_dir / "repomd.xml").write_text(filtered)
        if failed:
            return "пропущены метаданные: " + ", ".join(failed[:6])
        return ""

    async def update_metadata(self, repo_path: str) -> Tuple[bool, str]:
        path = Path(repo_path)
        path.mkdir(parents=True, exist_ok=True)
        keep = (path / "repodata" / "repomd.xml").is_file()
        cmd = [self.createrepo, "--update", str(path)]
        if keep:
            cmd.insert(1, "--keep-all-metadata")
        code, out, err = await run_cmd(cmd)
        if code != 0 and keep and "keep-all-metadata" in (err or out or ""):
            code, out, err = await run_cmd([self.createrepo, "--update", str(path)])
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
