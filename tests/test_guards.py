import ast
import unittest
from pathlib import Path

from app.guards import inside, metadata_error, mirror_field_error, name_error, publish_arch_flag, tar_member_error


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

    def test_metadata_error(self):
        text = (
            "repodata/e7867d26196f82c4aeb71ce1dc5588a9f5961432d8494c095c579a31df896abd-primary.xml.gz - Cannot download; "
            "repodata/11257b840dec0d0d4d82b3fc12e362e09352b669be67cc3f6fb510c114ba5b19-primary.sqlite.bz2 - Cannot download"
        )
        self.assertEqual(metadata_error(text), "метаданные не скачаны: primary.xml.gz, primary.sqlite.bz2")

    def test_inside(self):
        root = Path("/var/lib/repo-manager/mirrors")
        self.assertTrue(inside(root / "rpm" / "OL10", root))
        self.assertFalse(inside(root / "rpm" / ".." / ".." / "etc", root))


if __name__ == "__main__":
    unittest.main()
