import ast
import re
import shutil
import tempfile
import unittest
from pathlib import Path


def _load(rel, names, consts=()):
    path = Path(__file__).resolve().parents[1] / rel
    tree = ast.parse(path.read_text())
    keep = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            keep.append(node)
        elif isinstance(node, ast.Assign):
            ids = [target.id for target in node.targets if isinstance(target, ast.Name)]
            if any(item in consts for item in ids):
                keep.append(node)
    ns = {"Path": Path, "shutil": shutil, "re": re}
    exec(compile(ast.Module(keep, []), str(path), "exec"), ns)
    return ns


SYNC = _load("app/api/sync.py", {"plan_starts", "should_queue"})
RPM = _load("app/services/rpm.py", {"proc_cmdlines", "createrepo_busy", "drop_stale_repodata"})
MIRRORS = _load("app/api/mirrors.py", {"_package_arch_ok"})
PROXY = _load("app/services/proxy.py", {"redact_proxy_url", "restore_proxy_url"}, {"_USERINFO"})


class PlanTests(unittest.TestCase):
    def test_second_deb_waits(self):
        start, back = SYNC["plan_starts"]([1, 2, 3], 0, 2, None, {1: "deb", 2: "deb", 3: "rpm"})
        self.assertEqual(start, [1, 3])
        self.assertEqual(back, [2])

    def test_deb_already_running_lets_rpm_through(self):
        start, back = SYNC["plan_starts"]([2, 3], 1, 2, 9, {2: "deb", 3: "rpm"})
        self.assertEqual(start, [3])
        self.assertEqual(back, [2])

    def test_limit_holds_the_rest(self):
        start, back = SYNC["plan_starts"]([1, 2], 2, 2, None, {1: "rpm", 2: "rpm"})
        self.assertEqual(start, [])
        self.assertEqual(back, [1, 2])

    def test_rpm_is_not_blocked_by_waiting_deb(self):
        queue = SYNC["should_queue"]
        self.assertFalse(queue(1, 5, "rpm", True, True, False))
        self.assertTrue(queue(1, 5, "deb", True, False, False))
        self.assertTrue(queue(5, 5, "rpm", False, False, False))


class RepodataTests(unittest.TestCase):
    def test_busy_when_createrepo_has_same_path(self):
        repo = Path("/var/lib/repo-manager/mirrors/rpm/OL10")
        lines = [f"createrepo_c --update {repo}"]
        self.assertTrue(RPM["createrepo_busy"](repo, lines))
        self.assertFalse(RPM["createrepo_busy"](repo, ["createrepo_c --update /other"]))

    def test_stale_dir_removed_when_idle(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".repodata").mkdir()
            self.assertIsNone(RPM["drop_stale_repodata"](repo, []))
            self.assertFalse((repo / ".repodata").exists())

    def test_live_createrepo_is_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".repodata").mkdir()
            note = RPM["drop_stale_repodata"](repo, [f"createrepo_c --update {repo}"])
            self.assertIn("ещё работает", note)
            self.assertTrue((repo / ".repodata").is_dir())


class ProxyTests(unittest.TestCase):
    def test_password_hidden_and_restored(self):
        raw = "http://user:secret@proxy:3128"
        shown = PROXY["redact_proxy_url"](raw)
        self.assertNotIn("secret", shown)
        self.assertEqual(PROXY["restore_proxy_url"](raw, shown), raw)
        self.assertEqual(PROXY["restore_proxy_url"](raw, "http://user:new@proxy:3128"), "http://user:new@proxy:3128")


class ArchTests(unittest.TestCase):
    def test_arm64_is_visible(self):
        ok = MIRRORS["_package_arch_ok"]
        self.assertTrue(ok("bash_5.2-1_arm64", "arm64"))
        self.assertTrue(ok("bash_5.2-1_all", "arm64"))
        self.assertFalse(ok("bash_5.2-1_amd64", "arm64"))


if __name__ == "__main__":
    unittest.main()
