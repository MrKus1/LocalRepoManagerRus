import ast
import unittest
from pathlib import Path

from app.guards import (
    fk_needs_rebuild, host_blocked, http_status_and_location, inside, metadata_error,
    mirror_field_error, name_error, next_hop, publish_arch_flag, shown_package_count,
    tar_member_error, url_error, aptly_show_mismatch,
    deb_index_names, indexes_from_release, sum_package_sizes, bytes_still_needed, space_block_message,
)


def _drop_ok():
    path = Path(__file__).resolve().parents[1] / "app" / "services" / "deb.py"
    src = path.read_text()
    tree = ast.parse(src)
    ns = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "mirror_drop_ok":
            exec(compile(ast.Module([node], []), "<deb>", "exec"), ns)
    return ns["mirror_drop_ok"]


class GuardTests(unittest.TestCase):
    def test_name(self):
        self.assertIsNone(name_error("OL10_BaseOs"))
        self.assertIsNone(name_error("Debian12Bookworm"))
        self.assertIsNotNone(name_error("../etc"))
        self.assertIsNotNone(name_error("a/b"))
        self.assertIsNotNone(name_error(""))

    def test_url_and_cron(self):
        self.assertIsNone(mirror_field_error({
            "source_url": "http://mirror.yandex.ru/oracle/OL10/baseos/latest/x86_64/",
            "schedule": "0 3 * * *",
            "distribution": "bookworm",
            "components": "main",
            "architectures": "x86_64",
        }))
        self.assertIsNotNone(mirror_field_error({"source_url": ""}))
        self.assertIsNotNone(mirror_field_error({"source_url": "file:///etc/passwd"}))
        self.assertIsNotNone(mirror_field_error({"schedule": "каждый день"}))
        self.assertIsNone(mirror_field_error({"schedule": ""}))

    def test_local_address_rejected(self):
        self.assertTrue(host_blocked("127.0.0.1"))
        self.assertTrue(host_blocked("169.254.169.254"))
        self.assertTrue(host_blocked("::1"))
        self.assertFalse(host_blocked("10.1.1.5"))
        self.assertFalse(host_blocked("192.168.3.9"))
        self.assertIsNotNone(url_error("http://127.0.0.1/repo", resolve=False))
        self.assertIsNotNone(url_error("http://[::1]/", resolve=False))
        self.assertIsNone(url_error("http://10.1.1.5/debian/", resolve=False))
        self.assertIsNone(url_error("http://mirror.yandex.ru/debian/", resolve=False))

    def test_fk_kept_when_cascade_exists(self):
        self.assertTrue(fk_needs_rebuild(None))
        self.assertTrue(fk_needs_rebuild("FOREIGN KEY (mirror_id) REFERENCES mirrors(id)"))
        self.assertFalse(fk_needs_rebuild(
            "FOREIGN KEY (mirror_id) REFERENCES mirrors(id) ON DELETE CASCADE"
        ))

    def test_cache_checks_redirect_itself(self):
        text = (Path(__file__).resolve().parents[1] / "app" / "api" / "cache.py").read_text()
        self.assertIn("follow_redirects=False", text)
        self.assertNotIn("follow_redirects=True", text)
        self.assertIn("url_error(current)", text)

    def test_tar_member(self):
        self.assertIsNone(tar_member_error("repo-manager/VERSION"))
        self.assertIsNotNone(tar_member_error("/etc/passwd"))
        self.assertIsNotNone(tar_member_error("repo-manager/../../etc/passwd"))
        self.assertIsNotNone(tar_member_error("other/VERSION"))

    def test_drop(self):
        drop = _drop_ok()
        self.assertTrue(drop(["mirror: 0 removed"]))
        self.assertTrue(drop(["mirror: 1 ERROR: mirror with name X not found"]))
        self.assertFalse(drop(["mirror: 1 ERROR: database locked"]))

    def test_publish_arch(self):
        self.assertEqual(publish_arch_flag("amd64"), "-architectures=amd64")
        self.assertEqual(publish_arch_flag("amd64, all"), "-architectures=amd64")
        self.assertEqual(publish_arch_flag(""), "-architectures=amd64")

    def test_aptly_show_can_be_restored(self):
        show = "\n".join([
            "Name: Debian12Bookworm",
            "Archive Root URL: http://mirror.yandex.ru/debian/",
            "Distribution: bookworm",
            "Components: main",
            "Architectures: amd64",
        ])
        self.assertIsNone(aptly_show_mismatch(show, "http://mirror.yandex.ru/debian", "bookworm", "main", "amd64"))
        self.assertIsNotNone(aptly_show_mismatch(show, "http://deb.debian.org/debian", "bookworm", "main", "amd64"))
        self.assertIsNotNone(aptly_show_mismatch(show, "http://mirror.yandex.ru/debian/", "trixie", "main", "amd64"))

    def test_shown_package_count_uses_disk(self):
        self.assertEqual(shown_package_count(1000, 0), 1000)
        self.assertEqual(shown_package_count(0, 12), 12)
        self.assertEqual(shown_package_count(None, 4), 4)

    def test_metadata_error(self):
        text = (
            "repodata/e7867d26196f82c4aeb71ce1dc5588a9f5961432d8494c095c579a31df896abd-primary.xml.gz - Cannot download; "
            "repodata/11257b840dec0d0d4d82b3fc12e362e09352b669be67cc3f6fb510c114ba5b19-primary.sqlite.bz2 - Cannot download"
        )
        self.assertEqual(metadata_error(text), "метаданные не скачаны: primary.xml.gz, primary.sqlite.bz2")

    def test_deb_estimate_and_free_space(self):
        self.assertEqual(deb_index_names("main", "amd64"), ["main/binary-amd64/Packages.gz"])
        release = "abc 10 main/binary-amd64/Packages.gz\n"
        self.assertEqual(indexes_from_release(release, "main", "amd64"), ["main/binary-amd64/Packages.gz"])
        first = "Package: bash\nVersion: 1\nArchitecture: amd64\nSize: 1000\n\nPackage: adduser\nVersion: 2\nArchitecture: all\nSize: 500\n"
        second = "Package: adduser\nVersion: 2\nArchitecture: all\nSize: 500\n\nPackage: coreutils\nArchitecture: arm64\nSize: 2000\n"
        self.assertEqual(sum_package_sizes([first, second]), (3, 3500))
        self.assertEqual(bytes_still_needed(1000, 400), 600)
        self.assertEqual(bytes_still_needed(1000, 5000), 0)
        self.assertIsNone(space_block_message(10 * 1024 ** 3, 1024 ** 3))
        text = space_block_message(1024 ** 3, 5 * 1024 ** 3)
        self.assertIn("Sync не запущен", text)
        self.assertIsNone(space_block_message(1024 ** 3, 0))

    def test_release_version_is_newer(self):
        path = Path(__file__).resolve().parents[1] / "app" / "api" / "update.py"
        tree = ast.parse(path.read_text())
        node = next(item for item in tree.body if isinstance(item, ast.FunctionDef) and item.name == "parse_version")
        ns = {}
        exec(compile(ast.Module([node], []), str(path), "exec"), ns)
        parse = ns["parse_version"]
        self.assertGreater(parse("1.2.29"), parse("1.2.28"))
        self.assertGreater(parse("1.2.28"), parse("1.2.27"))
        self.assertGreater(parse("1.2.25"), parse("1.2.24"))
        self.assertGreater(parse("1.2.24"), parse("1.2.23"))
        self.assertGreater(parse("1.2.23"), parse("1.2.22"))
        self.assertGreater(parse("1.2.22"), parse("1.2.21"))
        self.assertGreater(parse("1.2.21"), parse("1.2.20"))
        self.assertGreater(parse("1.2.20"), parse("1.2.19"))
        self.assertGreater(parse("1.2.19"), parse("1.2.18"))
        self.assertGreater(parse("1.2.18"), parse("1.2.17"))
        self.assertGreater(parse("1.2.17"), parse("1.2.16"))
        self.assertGreater(parse("1.2.16"), parse("1.2.15"))
        self.assertGreater(parse("1.2.15"), parse("1.2.14"))
        self.assertGreater(parse("1.2.14"), parse("1.2.13"))
        self.assertGreater(parse("1.2.13"), parse("1.2.12"))
        self.assertGreater(parse("1.2.12"), parse("1.2.11"))
        self.assertGreater(parse("1.2.11"), parse("1.2.10"))
        self.assertGreater(parse("1.2.10"), parse("1.2.9"))
        self.assertGreater(parse("1.2.9"), parse("1.2.8.15"))
        self.assertEqual(parse("1.2.10"), (1, 2, 10))
        self.assertEqual(
            next_hop("http://mirror.example/repo/", "http://127.0.0.1/secret"),
            (None, "URL указывает на локальный адрес"),
        )
        nxt, err = next_hop("http://mirror.example/repo/", "http://10.1.1.5/repo/")
        self.assertIsNone(err)
        self.assertEqual(nxt, "http://10.1.1.5/repo/")
        raw = "HTTP/1.1 301 Moved\nLocation: http://169.254.169.254/\n\nHTTP/1.1 200 OK\n"
        self.assertEqual(http_status_and_location(raw), (200, None))
        root = Path("/var/lib/repo-manager/mirrors")
        self.assertTrue(inside(root / "rpm" / "OL10", root))
        self.assertFalse(inside(root / "rpm" / ".." / ".." / "etc", root))


if __name__ == "__main__":
    unittest.main()
