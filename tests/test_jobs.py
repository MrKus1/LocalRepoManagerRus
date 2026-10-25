import ast
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace


def _load(names):
    path = Path(__file__).resolve().parents[1] / "app" / "services" / "jobs.py"
    tree = ast.parse(path.read_text())
    keep = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    ns = {"datetime": datetime}
    exec(compile(ast.Module(keep, []), str(path), "exec"), ns)
    return ns


JOBS = _load({"build_tasks", "_stamp", "_iso", "interrupt_job"})
build_tasks = JOBS["build_tasks"]


class JobTests(unittest.TestCase):
    def test_sync_is_visible_without_aptly_jobs(self):
        log = SimpleNamespace(id=4, mirror_id=1, status="success", started_at=datetime(2026, 10, 2, 12, 0))
        rows = build_tasks([], [(log, "OL10_BaseOs")], [])
        self.assertEqual(rows[0]["title"], "Sync OL10_BaseOs")
        self.assertEqual(rows[0]["status"], "success")
        self.assertEqual(rows[0]["started_at"], "02.10 12:00")

    def test_running_stays_above_old_success(self):
        old = SimpleNamespace(id=1, mirror_id=1, status="success", started_at=datetime(2026, 10, 2, 12, 0))
        fresh = SimpleNamespace(id=2, mirror_id=2, status="running", started_at=datetime(2026, 10, 2, 8, 0))
        rows = build_tasks([], [(old, "A"), (fresh, "B")], [])
        self.assertEqual(rows[0]["status"], "running")
        self.assertEqual(rows[1]["title"], "Sync A")

    def test_queue_and_no_duplicate_running(self):
        log = SimpleNamespace(id=3, mirror_id=7, status="running", started_at=datetime(2026, 10, 2, 9, 0))
        waiting = [
            SimpleNamespace(id=7, name="B", status="running", updated_at=datetime(2026, 10, 2, 9, 0)),
            SimpleNamespace(id=8, name="C", status="queued", updated_at=datetime(2026, 10, 2, 9, 1)),
        ]
        rows = build_tasks([], [(log, "B")], waiting)
        titles = [row["title"] for row in rows]
        self.assertEqual(titles.count("Sync B"), 1)
        self.assertIn("queued", [row["status"] for row in rows])

    def test_restart_marks_running_failed(self):
        job = JOBS["interrupt_job"]({"id": 4, "status": "running", "line": "запуск"})
        self.assertEqual(job["status"], "failed")
        self.assertIn("перезапуском", job["line"])
        done = JOBS["interrupt_job"]({"id": 5, "status": "success", "line": ""})
        self.assertEqual(done["status"], "success")


if __name__ == "__main__":
    unittest.main()
