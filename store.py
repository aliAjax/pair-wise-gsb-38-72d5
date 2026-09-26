"""数据层：SQLite 存储、领域校验和状态机。

只依赖标准库与 diffcalc 模块，不包含任何 HTTP 逻辑；页面通过 app.py 的
接口间接访问这里的方法。
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from diffcalc import compute_diff

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "subtitle_qc.db"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DomainError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class Database:
    def __init__(self, path: str | os.PathLike[str] = DEFAULT_DB):
        self.path = str(path)
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    source_language TEXT NOT NULL,
                    duration_ms INTEGER NOT NULL CHECK(duration_ms > 0),
                    owner TEXT NOT NULL,
                    media_name TEXT NOT NULL,
                    media_sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id INTEGER NOT NULL REFERENCES projects(id),
                    language TEXT NOT NULL,
                    version_no INTEGER NOT NULL,
                    parent_id INTEGER REFERENCES versions(id),
                    rework_reason TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'draft',
                    revision INTEGER NOT NULL DEFAULT 0,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(project_id,language,version_no)
                );
                CREATE TABLE IF NOT EXISTS version_baselines (
                    version_id INTEGER PRIMARY KEY REFERENCES versions(id) ON DELETE CASCADE,
                    parent_id INTEGER NOT NULL REFERENCES versions(id),
                    cues TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS assignments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
                    user TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('translator','timeline','reviewer')),
                    assigned_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(version_id,user,role)
                );
                CREATE TABLE IF NOT EXISTS cues (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
                    cue_index INTEGER NOT NULL,
                    start_ms INTEGER NOT NULL CHECK(start_ms >= 0),
                    end_ms INTEGER NOT NULL CHECK(end_ms > start_ms),
                    text TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(version_id,cue_index)
                );
                CREATE TABLE IF NOT EXISTS comments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
                    cue_id INTEGER REFERENCES cues(id) ON DELETE SET NULL,
                    user TEXT NOT NULL,
                    time_ms INTEGER NOT NULL CHECK(time_ms >= 0),
                    body TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS glossaries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    source_term TEXT NOT NULL,
                    required_translation TEXT NOT NULL,
                    forbidden_terms TEXT NOT NULL DEFAULT '[]',
                    notes TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    UNIQUE(project_id,source_term)
                );
                CREATE TABLE IF NOT EXISTS reviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
                    reviewer TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    comment TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS deliveries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    version_id INTEGER NOT NULL UNIQUE REFERENCES versions(id),
                    supersedes_version_id INTEGER REFERENCES versions(id),
                    snapshot_hash TEXT NOT NULL,
                    manifest TEXT NOT NULL,
                    delivered_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            # 旧库迁移：早期 versions 表没有 rework_reason 列。
            version_cols = {row["name"] for row in conn.execute("PRAGMA table_info(versions)")}
            if "rework_reason" not in version_cols:
                conn.execute("ALTER TABLE versions ADD COLUMN rework_reason TEXT NOT NULL DEFAULT ''")

    def _audit(self, conn: sqlite3.Connection, actor: str, action: str, entity_type: str,
               entity_id: int | None, details: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO audit_log(actor,action,entity_type,entity_id,details,created_at) VALUES(?,?,?,?,?,?)",
            (actor, action, entity_type, entity_id, json.dumps(details, ensure_ascii=False), utcnow()),
        )

    def create_project(self, actor: str, payload: dict[str, Any], role: str = "owner") -> dict[str, Any]:
        if role not in {"owner", "admin"}:
            raise DomainError("只有项目负责人可以创建项目", 403)
        name = str(payload.get("name", "")).strip()
        source_language = str(payload.get("source_language", "")).strip()
        media_name = str(payload.get("media_name", "")).strip()
        media_sha = str(payload.get("media_sha256", "")).lower()
        try:
            duration_ms = int(payload.get("duration_ms"))
        except (TypeError, ValueError) as exc:
            raise DomainError("成片时长必须是毫秒整数") from exc
        if not name or not source_language or not media_name or duration_ms <= 0 or len(media_sha) != 64:
            raise DomainError("项目名称、源语言、成片、时长或校验值不完整")
        with self.connect() as conn:
            try:
                cur = conn.execute(
                    "INSERT INTO projects(name,source_language,duration_ms,owner,media_name,media_sha256,created_at) VALUES(?,?,?,?,?,?,?)",
                    (name, source_language, duration_ms, actor, media_name, media_sha, utcnow()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("项目名称已存在", 409) from exc
            self._audit(conn, actor, "project.created", "project", cur.lastrowid, {"name": name})
            return dict(conn.execute("SELECT * FROM projects WHERE id=?", (cur.lastrowid,)).fetchone())

    def set_glossary(self, project_id: int, actor: str, payload: dict[str, Any], role: str = "viewer") -> dict[str, Any]:
        with self.connect() as conn:
            project = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
            if not project:
                raise DomainError("项目不存在", 404)
            if actor != project["owner"] and role != "admin":
                raise DomainError("只有项目负责人可以维护术语表", 403)
            source_term = str(payload.get("source_term", "")).strip()
            required = str(payload.get("required_translation", "")).strip()
            forbidden = payload.get("forbidden_terms", [])
            if not source_term or not required or not isinstance(forbidden, list):
                raise DomainError("术语、指定译法和禁用词格式不合法")
            conn.execute(
                """INSERT INTO glossaries(project_id,source_term,required_translation,forbidden_terms,notes,created_at)
                   VALUES(?,?,?,?,?,?) ON CONFLICT(project_id,source_term) DO UPDATE SET
                   required_translation=excluded.required_translation,forbidden_terms=excluded.forbidden_terms,notes=excluded.notes""",
                (project_id, source_term, required, json.dumps(forbidden, ensure_ascii=False), str(payload.get("notes", "")), utcnow()),
            )
            self._audit(conn, actor, "glossary.saved", "project", project_id, {"source_term": source_term})
        return {"project_id": project_id, "source_term": source_term, "required_translation": required, "forbidden_terms": forbidden}

    def create_version(self, project_id: int, actor: str, payload: dict[str, Any], role: str = "viewer") -> dict[str, Any]:
        language = str(payload.get("language", "")).strip()
        if not language:
            raise DomainError("目标语言不能为空")
        parent_id = payload.get("parent_id")
        rework_reason = str(payload.get("rework_reason", "")).strip()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            project = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
            if not project:
                raise DomainError("项目不存在", 404)
            if actor != project["owner"] and role != "admin":
                raise DomainError("只有项目负责人可以创建版本", 403)
            parent = None
            if parent_id is not None:
                # 先按 ID 找到父版本，再逐项说明为什么它不能作为基线，
                # 避免带着错误基线继续创建。
                parent = conn.execute("SELECT * FROM versions WHERE id=?", (int(parent_id),)).fetchone()
                if not parent:
                    raise DomainError(f"父版本 {parent_id} 不存在，不能作为返修基线", 404)
                if int(parent["project_id"]) != project_id:
                    raise DomainError(
                        f"父版本 {parent_id} 属于项目 {parent['project_id']}，与当前项目 {project_id} 不一致，不能作为返修基线", 409)
                if parent["language"] != language:
                    raise DomainError(
                        f"父版本语言为 {parent['language']}，与目标语言 {language} 不一致，不能作为返修基线", 409)
                if not rework_reason:
                    raise DomainError("基于父版本创建返修版本时必须填写返修原因")
            next_no = int(conn.execute("SELECT COALESCE(MAX(version_no),0)+1 value FROM versions WHERE project_id=? AND language=?", (project_id, language)).fetchone()["value"])
            cur = conn.execute(
                "INSERT INTO versions(project_id,language,version_no,parent_id,rework_reason,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (project_id, language, next_no, parent_id, rework_reason if parent else "", actor, utcnow(), utcnow()),
            )
            new_id = int(cur.lastrowid)
            copied = 0
            if parent is not None:
                # 复制父版本字幕作为新版本的起点，同时把复制时刻的父版本内容
                # 快照为返修基线，供差异计算在复核通过前反复使用。
                parent_cues = [dict(r) for r in conn.execute(
                    "SELECT cue_index,start_ms,end_ms,text FROM cues WHERE version_id=? ORDER BY cue_index", (parent["id"],))]
                for cue in parent_cues:
                    conn.execute(
                        "INSERT INTO cues(version_id,cue_index,start_ms,end_ms,text,updated_by,updated_at) VALUES(?,?,?,?,?,?,?)",
                        (new_id, cue["cue_index"], cue["start_ms"], cue["end_ms"], cue["text"], actor, utcnow()),
                    )
                conn.execute(
                    "INSERT INTO version_baselines(version_id,parent_id,cues,created_at) VALUES(?,?,?,?)",
                    (new_id, parent["id"], json.dumps(parent_cues, ensure_ascii=False), utcnow()),
                )
                copied = len(parent_cues)
            self._audit(conn, actor, "version.created", "version", new_id, {
                "language": language, "version_no": next_no,
                "parent_id": parent["id"] if parent else None,
                "rework_reason": rework_reason if parent else "",
                "copied_cues": copied,
            })
            return dict(conn.execute("SELECT * FROM versions WHERE id=?", (new_id,)).fetchone())

    def assign(self, version_id: int, actor: str, payload: dict[str, Any], role: str = "viewer") -> dict[str, Any]:
        user = str(payload.get("user", "")).strip()
        assignment_role = str(payload.get("role", "")).strip()
        if not user or assignment_role not in {"translator", "timeline", "reviewer"}:
            raise DomainError("人员或角色不合法")
        with self.connect() as conn:
            version = conn.execute("SELECT v.*,p.owner FROM versions v JOIN projects p ON p.id=v.project_id WHERE v.id=?", (version_id,)).fetchone()
            if not version:
                raise DomainError("版本不存在", 404)
            if actor != version["owner"] and role != "admin":
                raise DomainError("只有项目负责人可以分配人员", 403)
            conn.execute("INSERT OR IGNORE INTO assignments(version_id,user,role,assigned_by,created_at) VALUES(?,?,?,?,?)", (version_id, user, assignment_role, actor, utcnow()))
            self._audit(conn, actor, "assignment.saved", "version", version_id, {"user": user, "role": assignment_role})
        return {"version_id": version_id, "user": user, "role": assignment_role}

    def _version(self, conn: sqlite3.Connection, version_id: int) -> sqlite3.Row:
        row = conn.execute("SELECT v.*,p.owner,p.duration_ms FROM versions v JOIN projects p ON p.id=v.project_id WHERE v.id=?", (version_id,)).fetchone()
        if not row:
            raise DomainError("字幕版本不存在", 404)
        return row

    def _can_edit(self, conn: sqlite3.Connection, version: sqlite3.Row, actor: str) -> bool:
        if actor == version["owner"]:
            return True
        return bool(conn.execute("SELECT 1 FROM assignments WHERE version_id=? AND user=? AND role IN ('translator','timeline')", (version["id"], actor)).fetchone())

    def _validate_glossary(self, conn: sqlite3.Connection, project_id: int, text: str) -> None:
        for row in conn.execute("SELECT * FROM glossaries WHERE project_id=?", (project_id,)):
            forbidden = json.loads(row["forbidden_terms"])
            for term in forbidden:
                if term and term in text:
                    raise DomainError(f"字幕包含禁用译法: {term}")
            # The glossary is enforced only when the corresponding source term
            # appears in the localized cue. This keeps it useful without making
            # every cue repeat every glossary word.
            if row["source_term"] in text and row["required_translation"] not in text:
                raise DomainError(f"术语 {row['source_term']} 必须使用指定译法 {row['required_translation']}")

    def save_cue(self, version_id: int, actor: str, payload: dict[str, Any], role: str = "viewer") -> dict[str, Any]:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            version = self._version(conn, version_id)
            if version["status"] != "draft":
                raise DomainError("只有草稿版本可以修改字幕", 409)
            if not self._can_edit(conn, version, actor):
                raise DomainError("没有该版本的翻译或时间轴权限", 403)
            expected = payload.get("expected_revision")
            if expected is not None and int(expected) != int(version["revision"]):
                raise DomainError("版本已被其他成员修改，请刷新后重试", 409)
            try:
                cue_index = int(payload.get("cue_index"))
                start_ms = int(payload.get("start_ms"))
                end_ms = int(payload.get("end_ms"))
            except (TypeError, ValueError) as exc:
                raise DomainError("字幕序号和时间必须是整数") from exc
            text = str(payload.get("text", "")).strip()
            if cue_index < 0 or start_ms < 0 or end_ms <= start_ms or end_ms > int(version["duration_ms"]) or not text:
                raise DomainError("字幕时间、序号或内容不合法")
            self._validate_glossary(conn, int(version["project_id"]), text)
            cue_id = payload.get("cue_id")
            existing = None
            if cue_id is not None:
                existing = conn.execute("SELECT * FROM cues WHERE id=? AND version_id=?", (int(cue_id), version_id)).fetchone()
                if not existing:
                    raise DomainError("字幕条目不存在", 404)
            overlap = conn.execute(
                "SELECT * FROM cues WHERE version_id=? AND id<>? AND start_ms<? AND end_ms>? LIMIT 1",
                (version_id, int(cue_id or -1), end_ms, start_ms),
            ).fetchone()
            if overlap:
                raise DomainError("字幕时间轴发生重叠", 409)
            index_owner = conn.execute("SELECT * FROM cues WHERE version_id=? AND cue_index=? AND id<>?", (version_id, cue_index, int(cue_id or -1))).fetchone()
            if index_owner:
                raise DomainError("字幕序号已被使用", 409)
            if existing:
                conn.execute("UPDATE cues SET cue_index=?,start_ms=?,end_ms=?,text=?,updated_by=?,updated_at=? WHERE id=?", (cue_index, start_ms, end_ms, text, actor, utcnow(), existing["id"]))
                saved_id = existing["id"]
            else:
                cur = conn.execute("INSERT INTO cues(version_id,cue_index,start_ms,end_ms,text,updated_by,updated_at) VALUES(?,?,?,?,?,?,?)", (version_id, cue_index, start_ms, end_ms, text, actor, utcnow()))
                saved_id = cur.lastrowid
            revision = int(version["revision"]) + 1
            conn.execute("UPDATE versions SET revision=?,updated_at=? WHERE id=?", (revision, utcnow(), version_id))
            self._audit(conn, actor, "cue.saved", "version", version_id, {"cue_id": saved_id, "revision": revision})
        return dict(conn.execute("SELECT * FROM cues WHERE id=?", (saved_id,)).fetchone()) | {"version_revision": revision}

    def delete_cue(self, version_id: int, cue_id: int, actor: str, expected_revision: int | None = None) -> dict[str, Any]:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            version = self._version(conn, version_id)
            if version["status"] != "draft":
                raise DomainError("只有草稿版本可以修改字幕", 409)
            if not self._can_edit(conn, version, actor):
                raise DomainError("没有该版本的翻译或时间轴权限", 403)
            if expected_revision is not None and int(expected_revision) != int(version["revision"]):
                raise DomainError("版本已被其他成员修改，请刷新后重试", 409)
            cue = conn.execute("SELECT * FROM cues WHERE id=? AND version_id=?", (int(cue_id), version_id)).fetchone()
            if not cue:
                raise DomainError("字幕条目不存在", 404)
            conn.execute("DELETE FROM cues WHERE id=?", (int(cue_id),))
            revision = int(version["revision"]) + 1
            conn.execute("UPDATE versions SET revision=?,updated_at=? WHERE id=?", (revision, utcnow(), version_id))
            self._audit(conn, actor, "cue.deleted", "version", version_id, {"cue_id": int(cue_id), "revision": revision})
        return {"deleted": int(cue_id), "version_revision": revision}

    def add_comment(self, version_id: int, actor: str, payload: dict[str, Any], role: str = "viewer") -> dict[str, Any]:
        body = str(payload.get("body", "")).strip()
        try:
            time_ms = int(payload.get("time_ms"))
        except (TypeError, ValueError) as exc:
            raise DomainError("评论时间必须是毫秒整数") from exc
        with self.connect() as conn:
            version = self._version(conn, version_id)
            allowed = actor == version["owner"] or conn.execute("SELECT 1 FROM assignments WHERE version_id=? AND user=?", (version_id, actor)).fetchone()
            if not allowed:
                raise DomainError("只有项目成员可以评论", 403)
            if not body or time_ms < 0 or time_ms > int(version["duration_ms"]):
                raise DomainError("评论内容或时间点不合法")
            cue_id = payload.get("cue_id")
            if cue_id is not None and not conn.execute("SELECT 1 FROM cues WHERE id=? AND version_id=?", (int(cue_id), version_id)).fetchone():
                raise DomainError("评论关联的字幕不存在", 404)
            cur = conn.execute("INSERT INTO comments(version_id,cue_id,user,time_ms,body,created_at) VALUES(?,?,?,?,?,?)", (version_id, cue_id, actor, time_ms, body, utcnow()))
            self._audit(conn, actor, "comment.added", "version", version_id, {"comment_id": cur.lastrowid, "time_ms": time_ms})
        return {"id": int(cur.lastrowid), "version_id": version_id, "cue_id": cue_id, "user": actor, "time_ms": time_ms, "body": body, "status": "open"}

    def submit(self, version_id: int, actor: str, role: str = "viewer") -> dict[str, Any]:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            version = self._version(conn, version_id)
            if version["status"] != "draft" or not self._can_edit(conn, version, actor):
                raise DomainError("只有草稿版本的翻译或时间轴人员可以提交复核", 409)
            if not conn.execute("SELECT 1 FROM cues WHERE version_id=?", (version_id,)).fetchone():
                raise DomainError("空版本不能提交复核", 409)
            conn.execute("UPDATE versions SET status='review',updated_at=? WHERE id=?", (utcnow(), version_id))
            self._audit(conn, actor, "version.submitted", "version", version_id, {})
        return dict(conn.execute("SELECT * FROM versions WHERE id=?", (version_id,)).fetchone())

    def review(self, version_id: int, actor: str, payload: dict[str, Any], role: str = "viewer") -> dict[str, Any]:
        decision = str(payload.get("decision", "")).strip()
        if decision not in {"approve", "reject"}:
            raise DomainError("复核决定必须是 approve 或 reject")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            version = self._version(conn, version_id)
            if version["status"] != "review":
                raise DomainError("版本当前不在复核阶段", 409)
            assigned = conn.execute("SELECT 1 FROM assignments WHERE version_id=? AND user=? AND role='reviewer'", (version_id, actor)).fetchone()
            if not assigned and actor != version["owner"]:
                raise DomainError("没有该版本的复核权限", 403)
            if actor == version["created_by"]:
                raise DomainError("创建人不能复核自己的版本", 403)
            conn.execute("INSERT INTO reviews(version_id,reviewer,decision,comment,created_at) VALUES(?,?,?,?,?)", (version_id, actor, decision, str(payload.get("comment", "")), utcnow()))
            status = "approved" if decision == "approve" else "draft"
            conn.execute("UPDATE versions SET status=?,updated_at=? WHERE id=?", (status, utcnow(), version_id))
            self._audit(conn, actor, f"version.{decision}", "version", version_id, {"comment": payload.get("comment", "")})
        return dict(conn.execute("SELECT * FROM versions WHERE id=?", (version_id,)).fetchone())

    def lock(self, version_id: int, actor: str, role: str = "viewer") -> dict[str, Any]:
        with self.connect() as conn:
            version = self._version(conn, version_id)
            if version["status"] != "approved":
                raise DomainError("只有已批准版本可以锁定", 409)
            if actor != version["owner"] and role != "admin":
                raise DomainError("只有项目负责人可以锁定版本", 403)
            conn.execute("UPDATE versions SET status='locked',updated_at=? WHERE id=?", (utcnow(), version_id))
            self._audit(conn, actor, "version.locked", "version", version_id, {})
        return dict(conn.execute("SELECT * FROM versions WHERE id=?", (version_id,)).fetchone())

    def deliver(self, version_id: int, actor: str, role: str = "viewer") -> dict[str, Any]:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            version = self._version(conn, version_id)
            if actor != version["owner"] and role != "admin":
                raise DomainError("只有项目负责人可以交付", 403)
            if version["status"] not in {"approved", "locked"}:
                raise DomainError("只有批准或锁定版本可以交付", 409)
            if conn.execute("SELECT 1 FROM deliveries WHERE version_id=?", (version_id,)).fetchone():
                raise DomainError("该版本已经交付，不能用新内容覆盖", 409)
            cues = [dict(r) for r in conn.execute("SELECT cue_index,start_ms,end_ms,text FROM cues WHERE version_id=? ORDER BY cue_index", (version_id,))]
            glossary = [dict(r) for r in conn.execute("SELECT source_term,required_translation,forbidden_terms FROM glossaries WHERE project_id=? ORDER BY source_term", (version["project_id"],))]
            manifest = {"project_id": version["project_id"], "version_id": version_id, "language": version["language"], "version_no": version["version_no"], "cues": cues, "glossary": glossary}
            snapshot_hash = hashlib.sha256(json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            previous = conn.execute("SELECT id FROM deliveries WHERE version_id IN (SELECT id FROM versions WHERE project_id=? AND language=? AND id<>?) ORDER BY id DESC LIMIT 1", (version["project_id"], version["language"], version_id)).fetchone()
            if previous:
                conn.execute("UPDATE versions SET status='superseded',updated_at=? WHERE id=(SELECT version_id FROM deliveries WHERE id=?)", (utcnow(), previous["id"]))
            cur = conn.execute(
                "INSERT INTO deliveries(version_id,supersedes_version_id,snapshot_hash,manifest,delivered_by,created_at) VALUES(?,?,?,?,?,?)",
                (version_id, previous["id"] if previous else None, snapshot_hash, json.dumps(manifest, ensure_ascii=False, sort_keys=True), actor, utcnow()),
            )
            conn.execute("UPDATE versions SET status='delivered',updated_at=? WHERE id=?", (utcnow(), version_id))
            self._audit(conn, actor, "version.delivered", "version", version_id, {"snapshot_hash": snapshot_hash})
        return dict(conn.execute("SELECT * FROM deliveries WHERE id=?", (cur.lastrowid,)).fetchone())

    def list_projects(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM projects ORDER BY id").fetchall()]

    def list_versions(self, project_id: int | None = None) -> list[dict[str, Any]]:
        with self.connect() as conn:
            if project_id:
                rows = conn.execute("SELECT * FROM versions WHERE project_id=? ORDER BY id", (project_id,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM versions ORDER BY id").fetchall()
            return [dict(r) for r in rows]

    def list_cues(self, version_id: int) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM cues WHERE version_id=? ORDER BY cue_index", (version_id,)).fetchall()]

    def list_comments(self, version_id: int) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM comments WHERE version_id=? ORDER BY id", (version_id,)).fetchall()]

    def list_reviews(self, version_id: int) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM reviews WHERE version_id=? ORDER BY id", (version_id,)).fetchall()]

    def list_deliveries(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM deliveries ORDER BY id DESC").fetchall()]

    def version_diff(self, version_id: int) -> dict[str, Any]:
        """返回该版本相对父版本基线的新增、改写和移除句子。

        基线在创建返修版本时快照，复核通过前一直保留，期间每次打开
        都按当前字幕重新计算，结果稳定可查。
        """
        with self.connect() as conn:
            version = self._version(conn, version_id)
            baseline = conn.execute("SELECT * FROM version_baselines WHERE version_id=?", (version_id,)).fetchone()
            if not baseline:
                raise DomainError("该版本没有父版本基线，无法计算返修差异", 404)
            current = [dict(r) for r in conn.execute(
                "SELECT cue_index,start_ms,end_ms,text FROM cues WHERE version_id=? ORDER BY cue_index", (version_id,))]
            diff = compute_diff(json.loads(baseline["cues"]), current)
            return {
                "version_id": version_id,
                "parent_id": int(baseline["parent_id"]),
                "rework_reason": version["rework_reason"],
                "status": version["status"],
                "baseline_at": baseline["created_at"],
                **diff,
            }

    def audit(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM audit_log ORDER BY id DESC").fetchall()]
