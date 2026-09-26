import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from app import Database, DomainError, Handler, seed_demo
from diffcalc import compute_diff
from http.server import ThreadingHTTPServer


class ReworkVersionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.db")
        seed = seed_demo(self.db)
        self.project, self.version = seed["project"], seed["version"]
        self.db.assign(self.version, "alice", {"user": "bob", "role": "translator"}, "owner")
        self.db.assign(self.version, "alice", {"user": "carol", "role": "reviewer"}, "owner")
        self.db.save_cue(self.version, "bob", {"cue_index": 1, "start_ms": 1000, "end_ms": 3000, "text": "海豹在冰面", "expected_revision": 0})
        self.db.save_cue(self.version, "bob", {"cue_index": 2, "start_ms": 4000, "end_ms": 6000, "text": "远处是雪山", "expected_revision": 1})
        self.db.submit(self.version, "bob")
        self.db.review(self.version, "carol", {"decision": "approve", "comment": "首版通过"}, "reviewer")

    def tearDown(self):
        self.tmp.cleanup()

    def _rework(self, reason="客户要求统一术语译法"):
        version = self.db.create_version(self.project, "alice", {
            "language": "zh-CN", "parent_id": self.version, "rework_reason": reason}, "owner")
        self.db.assign(version["id"], "alice", {"user": "bob", "role": "translator"}, "owner")
        self.db.assign(version["id"], "alice", {"user": "carol", "role": "reviewer"}, "owner")
        return version

    def test_rework_copies_parent_cues_and_keeps_reason(self):
        version = self._rework()
        self.assertEqual(version["parent_id"], self.version)
        self.assertEqual(version["rework_reason"], "客户要求统一术语译法")
        self.assertEqual(version["version_no"], 2)
        cues = self.db.list_cues(version["id"])
        self.assertEqual([(c["cue_index"], c["text"]) for c in cues], [(1, "海豹在冰面"), (2, "远处是雪山")])
        diff = self.db.version_diff(version["id"])
        self.assertEqual(diff["unchanged"], 2)
        self.assertEqual(diff["added"], [])
        self.assertEqual(diff["modified"], [])
        self.assertEqual(diff["removed"], [])

    def test_diff_tracks_added_modified_removed_until_approved(self):
        version = self._rework()
        cues = self.db.list_cues(version["id"])
        # 改写第 1 句，移除第 2 句，新增第 3 句。
        self.db.save_cue(version["id"], "bob", {
            "cue_id": cues[0]["id"], "cue_index": 1, "start_ms": 1000, "end_ms": 3500,
            "text": "海豹出现在冰面上", "expected_revision": 0})
        self.db.delete_cue(version["id"], cues[1]["id"], "bob", expected_revision=1)
        self.db.save_cue(version["id"], "bob", {
            "cue_index": 3, "start_ms": 7000, "end_ms": 9000, "text": "科考队出发", "expected_revision": 2})
        diff = self.db.version_diff(version["id"])
        self.assertEqual([c["text"] for c in diff["added"]], ["科考队出发"])
        self.assertEqual(len(diff["modified"]), 1)
        self.assertEqual(diff["modified"][0]["cue_index"], 1)
        self.assertEqual(diff["modified"][0]["before"]["text"], "海豹在冰面")
        self.assertEqual(diff["modified"][0]["after"]["text"], "海豹出现在冰面上")
        self.assertEqual(diff["modified"][0]["changes"], ["end_ms", "text"])
        self.assertEqual([c["text"] for c in diff["removed"]], ["远处是雪山"])
        self.assertEqual(diff["unchanged"], 0)
        # 提交复核后、复核通过前差异仍然保留。
        self.db.submit(version["id"], "bob")
        pending = self.db.version_diff(version["id"])
        self.assertEqual(pending["status"], "review")
        self.assertEqual(len(pending["added"]), 1)
        self.assertEqual(len(pending["removed"]), 1)
        self.db.review(version["id"], "carol", {"decision": "approve", "comment": "返修通过"}, "reviewer")

    def test_parent_must_match_project_and_language(self):
        other = self.db.create_project("alice", {
            "name": "另一部片子", "source_language": "en", "media_name": "other.mp4",
            "media_sha256": "c" * 64, "duration_ms": 60000}, "owner")
        with self.assertRaisesRegex(DomainError, "属于项目"):
            self.db.create_version(other["id"], "alice", {
                "language": "zh-CN", "parent_id": self.version, "rework_reason": "跨项目"}, "owner")
        with self.assertRaisesRegex(DomainError, "语言为 zh-CN"):
            self.db.create_version(self.project, "alice", {
                "language": "ja", "parent_id": self.version, "rework_reason": "跨语言"}, "owner")
        with self.assertRaisesRegex(DomainError, "返修原因"):
            self.db.create_version(self.project, "alice", {
                "language": "zh-CN", "parent_id": self.version}, "owner")
        with self.assertRaisesRegex(DomainError, "不存在"):
            self.db.create_version(self.project, "alice", {
                "language": "zh-CN", "parent_id": 9999, "rework_reason": "基线缺失"}, "owner")
        # 错误基线不会留下半成品版本。
        self.assertEqual(len(self.db.list_versions(self.project)), 1)

    def test_history_stays_queryable_after_rework_delivery(self):
        self.db.lock(self.version, "alice")
        first_delivery = self.db.deliver(self.version, "alice")
        version = self._rework()
        self.db.save_cue(version["id"], "bob", {
            "cue_index": 3, "start_ms": 7000, "end_ms": 9000, "text": "科考队出发", "expected_revision": 0})
        self.db.submit(version["id"], "bob")
        self.db.review(version["id"], "carol", {"decision": "approve", "comment": "返修通过"}, "reviewer")
        self.db.lock(version["id"], "alice")
        second = self.db.deliver(version["id"], "alice")
        # 旧版本、原复核结论和交付快照都完整可查。
        versions = {v["id"]: v for v in self.db.list_versions(self.project)}
        self.assertEqual(versions[self.version]["status"], "superseded")
        self.assertEqual(versions[version["id"]]["status"], "delivered")
        self.assertEqual([r["comment"] for r in self.db.list_reviews(self.version)], ["首版通过"])
        self.assertEqual([r["comment"] for r in self.db.list_reviews(version["id"])], ["返修通过"])
        deliveries = self.db.list_deliveries()
        self.assertEqual(len(deliveries), 2)
        self.assertEqual(second["supersedes_version_id"], self.version)
        old_manifest = json.loads(next(d["manifest"] for d in deliveries if d["id"] == first_delivery["id"]))
        self.assertEqual([c["text"] for c in old_manifest["cues"]], ["海豹在冰面", "远处是雪山"])
        self.assertEqual(len(self.db.list_cues(self.version)), 2)

    def test_diff_requires_parent_baseline(self):
        with self.assertRaisesRegex(DomainError, "没有父版本基线"):
            self.db.version_diff(self.version)

    def test_delete_cue_guards(self):
        version = self._rework()
        cues = self.db.list_cues(version["id"])
        with self.assertRaisesRegex(DomainError, "权限"):
            self.db.delete_cue(version["id"], cues[0]["id"], "carol")
        with self.assertRaisesRegex(DomainError, "其他成员修改"):
            self.db.delete_cue(version["id"], cues[0]["id"], "bob", expected_revision=9)
        with self.assertRaisesRegex(DomainError, "不存在"):
            self.db.delete_cue(version["id"], 9999, "bob")
        self.db.submit(version["id"], "bob")
        with self.assertRaisesRegex(DomainError, "只有草稿"):
            self.db.delete_cue(version["id"], cues[0]["id"], "bob")


class DiffCalcTest(unittest.TestCase):
    def test_compute_diff_aligns_by_cue_index(self):
        baseline = [
            {"cue_index": 1, "start_ms": 0, "end_ms": 1000, "text": "甲"},
            {"cue_index": 2, "start_ms": 1000, "end_ms": 2000, "text": "乙"},
        ]
        current = [
            {"cue_index": 1, "start_ms": 0, "end_ms": 1000, "text": "甲"},
            {"cue_index": 2, "start_ms": 1000, "end_ms": 2500, "text": "乙改"},
            {"cue_index": 3, "start_ms": 3000, "end_ms": 4000, "text": "丙"},
        ]
        diff = compute_diff(baseline, current)
        self.assertEqual(diff["unchanged"], 1)
        self.assertEqual([m["cue_index"] for m in diff["modified"]], [2])
        self.assertEqual(diff["modified"][0]["changes"], ["end_ms", "text"])
        self.assertEqual([a["text"] for a in diff["added"]], ["丙"])
        current_without_2 = [c for c in current if c["cue_index"] != 2]
        self.assertEqual([r["text"] for r in compute_diff(baseline, current_without_2)["removed"]], ["乙"])


class HttpReworkTest(unittest.TestCase):
    """HTTP 层冒烟：验证 diff / reviews / 删除字幕的路由。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        Handler.db = Database(Path(cls.tmp.name) / "http.db")
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        seed = seed_demo(Handler.db)
        cls.project, cls.version = seed["project"], seed["version"]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def _call(self, method, path, body=None, user="alice", role="owner"):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json", "X-User": user, "X-Role": role})
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_rework_flow_over_http(self):
        Handler.db.assign(self.version, "alice", {"user": "bob", "role": "translator"}, "owner")
        status, cue = self._call("POST", f"/api/versions/{self.version}/cues", {
            "cue_index": 1, "start_ms": 1000, "end_ms": 3000, "text": "海豹在冰面"}, user="bob", role="translator")
        self.assertEqual(status, 201)
        status, created = self._call("POST", f"/api/projects/{self.project}/versions", {
            "language": "zh-CN", "parent_id": self.version, "rework_reason": "补译第二句"})
        self.assertEqual(status, 201)
        self.assertEqual(created["parent_id"], self.version)
        new_id = created["id"]
        status, diff = self._call("GET", f"/api/versions/{new_id}/diff")
        self.assertEqual(status, 200)
        self.assertEqual(diff["unchanged"], 1)
        status, cues = self._call("GET", f"/api/versions/{new_id}/cues")
        self.assertEqual(status, 200)
        copied_id = cues["cues"][0]["id"]
        status, _ = self._call("DELETE", f"/api/versions/{new_id}/cues/{cue['id']}", {})
        self.assertEqual(status, 404)  # 该字幕属于父版本，不在新版本里
        status, _ = self._call("DELETE", f"/api/versions/{new_id}/cues/{copied_id}", {})
        self.assertEqual(status, 200)
        status, diff = self._call("GET", f"/api/versions/{new_id}/diff")
        self.assertEqual([r["text"] for r in diff["removed"]], ["海豹在冰面"])
        status, err = self._call("GET", f"/api/versions/{self.version}/diff")
        self.assertEqual(status, 404)
        status, reviews = self._call("GET", f"/api/versions/{new_id}/reviews")
        self.assertEqual(status, 200)
        self.assertEqual(reviews["reviews"], [])


if __name__ == "__main__":
    unittest.main()
