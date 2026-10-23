import unittest
from datetime import datetime
from types import SimpleNamespace

from app.services.jobs import build_tasks


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


if __name__ == "__main__":
    unittest.main()
