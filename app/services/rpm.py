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
from app.guards import http_status_and_location, metadata_error, next_hop, url_error

logger = logging.getLogger(__name__)


def proc_cmdlines() -> list[str]:
    lines = []
    root = Path("/proc")
    if not root.is_dir():
        return lines
    for entry in root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        if raw:
            lines.append(raw.replace(b"\0", b" ").decode(errors="replace"))
    return lines


def createrepo_busy(repo: Path, cmdlines: list[str] | None = None) -> bool:
    target = str(repo)
    lines = proc_cmdlines() if cmdlines is None else cmdlines
    return any("createrepo" in line and target in line for line in lines)


def drop_stale_repodata(repo: Path, cmdlines: list[str] | None = None) -> str | None:
    """Убирает брошенный каталог createrepo. Живой процесс не трогает."""
    stale = repo / ".repodata"
    if not stale.is_dir():
        return None
    if createrepo_busy(repo, cmdlines):
        return "Temporary repodata already exists: createrepo ещё работает"
    shutil.rmtree(stale)
    return None


async def run_cmd(cmd: list[str], cwd: Optional[str] = None) -> Tuple[int, str, str]:
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


async def _curl_once(url: str, dest: str | None) -> tuple[int, str]:
    """Один запрос без -L. Заголовки в stdout, тело в dest или в никуда."""
    from app.services.proxy import proxy_env
    cmd = ["curl", "-sS", "-D", "-", "--max-redirs", "0", "--max-time", "180", url]
    if dest:
        cmd[1:1] = ["-o", dest]
    else:
        cmd[1:1] = ["-o", "/dev/null"]
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env=proxy_env(),
    )
    out, _ = await process.communicate()
    return process.returncode or 0, out.decode(errors="replace")


async def download_checked(url: str, dest: Path | None = None) -> tuple[str | None, str | None]:
    """Идёт по редиректам сам. На loopback и link-local останавливается и не сохраняет файл."""
    current = url
    for _ in range(5):
        err = url_error(current)
        if err:
            if dest:
                dest.unlink(missing_ok=True)
            return None, err
        code, headers = await _curl_once(current, str(dest) if dest else None)
        status, location = http_status_and_location(headers)
        if status == 200 and code == 0:
            return current, None
        if dest:
            dest.unlink(missing_ok=True)
        if status in (301, 302, 303, 307, 308) and location:
            nxt, hop_err = next_hop(current, location)
            if hop_err or not nxt:
                return None, hop_err or "редирект без адреса"
            current = nxt
            continue
        if code != 0 and status is None:
            return None, f"нет ответа: {current}"
        return None, f"http={status or 'нет'}: {current}"
    if dest:
        dest.unlink(missing_ok=True)
    return None, "слишком много перенаправлений"


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
        private = settings.STORAGE_ROOT / "tmp" / "dnf" / dest.name
        private.mkdir(parents=True, exist_ok=True)
        cmd = [
            "dnf",
            "--exclude=*.src.rpm",
            "--disablerepo=*",
            "--nogpgcheck",
            "--setopt=fastestmirror=0",
            "--setopt=retries=8",
            "--setopt=timeout=180",
            "--setopt=max_parallel_downloads=4",
            f"--setopt=cachedir={private / 'cache'}",
            f"--setopt=persistdir={private / 'persist'}",
            f"--setopt=logdir={private / 'log'}",
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
            final, err = await download_checked(source + "repodata/repomd.xml")
            if err or not final:
                return False, f"repomd.xml недоступен: {source}repodata/repomd.xml {err or ''}".strip()
            source = final.rsplit("repodata/", 1)[0]
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
            final, err = await download_checked(source + "repodata/repomd.xml", Path(tmp.name))
            if err or not final:
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
            code_ok, hop_err = await download_checked(source + href, target)
            if hop_err or not code_ok:
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
        stale = drop_stale_repodata(path)
        if stale:
            return False, stale
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
