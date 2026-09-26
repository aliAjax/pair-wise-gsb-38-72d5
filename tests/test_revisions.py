import tempfile
import unittest
from pathlib import Path

from app import Database, DomainError


class RevisionFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "rev.db")
        self.project = self.db.create_project("alice", {
            "name": "返修纪录片", "source_language": "en", "media_name": "polar.mp4",
            "media_sha256": "a" * 64, "duration_ms": 120000,
        }, "owner")["id"]
        self.project2 = self.db.create_project("alice", {
            "name": "另一部片子", "source_language": "en", "media_name": "ice.mp4",
            "media_sha256": "c" * 64, "duration_ms": 90000,
        }, "owner")["id"]
        self.v1 = self.db.create_version(self.project, "alice", {"language": "zh-CN"}, "owner")["id"]
        self.db.assign(self.v1, "alice", {"user": "bob", "role": "translator"}, "owner")
        self.db.assign(self.v1, "alice", {"user": "carol", "role": "reviewer"}, "owner")
        self.cue1 = self.db.save_cue(self.v1, "bob", {
            "cue_index": 1, "start_ms": 1000, "end_ms": 3000,
            "text": "海豹出现在冰面", "expected_revision": 0,
        })
        self.cue2 = self.db.save_cue(self.v1, "bob", {
            "cue_index": 2, "start_ms": 4000, "end_ms": 6000,
            "text": "第二句保持不动", "expected_revision": 1,
        })
        self.db.submit(self.v1, "bob")
        self.db.review(self.v1, "carol", {"decision": "approve", "comment": "v1 通过"}, "reviewer")
        self.db.add_comment(self.v1, "carol", {"cue_id": self.cue1["id"], "time_ms": 1200, "body": "原复核意见留档"}, "reviewer")

    def tearDown(self):
        self.tmp.cleanup()

    def _revision(self, reason="第 1 句返修"):
        rev = self.db.create_version(self.project, "alice", {
            "language": "zh-CN", "parent_id": self.v1, "revision_reason": reason,
        }, "owner")
        self.db.assign(rev["id"], "alice", {"user": "bob", "role": "translator"}, "owner")
        self.db.assign(rev["id"], "alice", {"user": "carol", "role": "reviewer"}, "owner")
        return rev

    def test_revision_copies_parent_cues_and_keeps_reason(self):
        rev = self._revision("术语返修")
        self.assertEqual(rev["parent_id"], self.v1)
        self.assertEqual(rev["revision_reason"], "术语返修")
        self.assertEqual(rev["version_no"], 2)
        cues = self.db.list_cues(rev["id"])
        self.assertEqual(len(cues), 2)
        self.assertEqual(cues[0]["text"], "海豹出现在冰面")
        self.assertEqual(cues[0]["cue_index"], 1)

    def test_revision_requires_reason(self):
        with self.assertRaisesRegex(DomainError, "返修原因"):
            self.db.create_version(self.project, "alice",
                                   {"language": "zh-CN", "parent_id": self.v1}, "owner")

    def test_parent_validation_messages(self):
        with self.assertRaisesRegex(DomainError, "不存在"):
            self.db.create_version(self.project, "alice",
                                   {"language": "zh-CN", "parent_id": 9999, "revision_reason": "x"}, "owner")
        other_lang = self.db.create_version(self.project, "alice", {"language": "ja"}, "owner")["id"]
        with self.assertRaisesRegex(DomainError, "语言是 ja"):
            self.db.create_version(self.project, "alice",
                                   {"language": "zh-CN", "parent_id": other_lang, "revision_reason": "x"}, "owner")
        other_project = self.db.create_version(self.project2, "alice", {"language": "zh-CN"}, "owner")["id"]
        with self.assertRaisesRegex(DomainError, "另一部片子"):
            self.db.create_version(self.project, "alice",
                                   {"language": "zh-CN", "parent_id": other_project, "revision_reason": "x"}, "owner")

    def test_diff_added_modified_removed_and_unchanged(self):
        rev_id = self._revision()["id"]
        # Rewrite cue 1 text, add cue 3, remove cue 2.
        self.db.save_cue(rev_id, "bob", {
            "cue_id": self.db.list_cues(rev_id)[0]["id"],
            "cue_index": 1, "start_ms": 1000, "end_ms": 3000,
            "text": "海豹跃上冰面", "expected_revision": 0,
        })
        self.db.save_cue(rev_id, "bob", {
            "cue_id": self.db.list_cues(rev_id)[1]["id"],
            "cue_index": 3, "start_ms": 7000, "end_ms": 8000,
            "text": "新增第三句", "expected_revision": 1,
        })
        diff = self.db.version_diff(rev_id)["diff"]
        self.assertEqual([c["cue_index"] for c in diff["added"]], [3])
        self.assertEqual([c["cue_index"] for c in diff["removed"]], [2])
        self.assertEqual(diff["modified"][0]["before"]["text"], "海豹出现在冰面")
        self.assertEqual(diff["modified"][0]["after"]["text"], "海豹跃上冰面")
        self.assertEqual(diff["modified"][0]["changes"], ["text"])

    def test_diff_retained_until_approval_then_closed(self):
        rev_id = self._revision()["id"]
        self.db.save_cue(rev_id, "bob", {
            "cue_id": self.db.list_cues(rev_id)[0]["id"],
            "cue_index": 1, "start_ms": 1000, "end_ms": 3000,
            "text": "海豹跃上冰面", "expected_revision": 0,
        })
        self.assertTrue(self.db.version_diff(rev_id)["retained"])
        self.db.assign(rev_id, "alice", {"user": "carol", "role": "reviewer"}, "owner")
        self.db.submit(rev_id, "bob")
        self.db.review(rev_id, "carol", {"decision": "reject", "comment": "再改"}, "reviewer")
        self.assertTrue(self.db.version_diff(rev_id)["retained"])
        self.db.submit(rev_id, "bob")
        self.db.review(rev_id, "carol", {"decision": "approve", "comment": "通过"}, "reviewer")
        self.assertFalse(self.db.version_diff(rev_id)["retained"])
        # Diff remains available historically.
        self.assertEqual(self.db.version_diff(rev_id)["diff"]["summary"]["modified"], 1)

    def test_old_version_reviews_comments_and_snapshots_remain(self):
        self.db.lock(self.v1, "alice")
        delivery = self.db.deliver(self.v1, "alice")
        rev_id = self._revision()["id"]
        # Parent version, its review record and comments are untouched.
        parent_detail = self.db.get_version(self.v1)
        self.assertEqual(parent_detail["status"], "delivered")
        self.assertEqual(parent_detail["latest_review"]["comment"], "v1 通过")
        self.assertEqual(len(self.db.list_reviews(self.v1)), 1)
        self.assertEqual(self.db.list_comments(self.v1)[0]["body"], "原复核意见留档")
        # Child carries no copied reviews/comments.
        self.assertEqual(self.db.list_reviews(rev_id), [])
        self.assertEqual(self.db.list_comments(rev_id), [])
        # Snapshot hash still resolves and the manifest keeps old cues.
        deliveries = {d["version_id"]: d for d in self.db.list_deliveries()}
        self.assertEqual(deliveries[self.v1]["snapshot_hash"], delivery["snapshot_hash"])
        self.assertIn("海豹出现在冰面", deliveries[self.v1]["manifest"])

    def test_get_version_includes_parent_and_reason(self):
        rev_id = self._revision()["id"]
        detail = self.db.get_version(rev_id)
        self.assertEqual(detail["parent"]["id"], self.v1)
        self.assertEqual(detail["parent"]["language"], "zh-CN")
        self.assertEqual(detail["revision_reason"], "第 1 句返修")
        self.assertIsNone(self.db.get_version(self.v1)["parent"])


if __name__ == "__main__":
    unittest.main()
