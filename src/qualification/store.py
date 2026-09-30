"""SQLite 持久化层。

关键约束：
- ``qualification_versions`` 只 INSERT，从不 UPDATE/DELETE —— 版本不可改写；
- 所有写操作集中在此处，领域层不接触 SQL；
- 敏感个人材料的每次读取尝试写入 ``access_log``，可追责。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import date, datetime
from pathlib import Path

from .models import (
    Case,
    Interval,
    Mentor,
    QualificationVersion,
    VerificationSnapshot,
)
from .timeutil import d, instant_iso, parse_date, parse_instant

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id   TEXT PRIMARY KEY,
    role      TEXT NOT NULL,
    name      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mentors (
    mentor_id   TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    id_digest   TEXT NOT NULL,
    phone       TEXT,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS qualification_versions (
    mentor_id        TEXT NOT NULL,
    version          INTEGER NOT NULL,
    specialties      TEXT NOT NULL,
    age_min          INTEGER NOT NULL,
    age_max          INTEGER NOT NULL,
    training_records TEXT NOT NULL,
    cert_source      TEXT NOT NULL,
    cert_no          TEXT NOT NULL,
    valid_from       TEXT NOT NULL,
    valid_until      TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    author           TEXT NOT NULL,
    PRIMARY KEY (mentor_id, version)
);
CREATE TABLE IF NOT EXISTS intervals (
    interval_id INTEGER PRIMARY KEY AUTOINCREMENT,
    mentor_id   TEXT NOT NULL,
    kind        TEXT NOT NULL,
    start_day   TEXT NOT NULL,
    end_day     TEXT NOT NULL,
    reason      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    author      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    scope         TEXT NOT NULL,
    mentor_id     TEXT NOT NULL,
    specialty     TEXT NOT NULL,
    audience_age  INTEGER NOT NULL,
    evaluated_at  TEXT NOT NULL,
    effective_day TEXT NOT NULL,
    decision      TEXT NOT NULL,
    reasons       TEXT NOT NULL,
    version       INTEGER,
    intervals     TEXT NOT NULL,
    created_by    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cases (
    case_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    mentor_id         TEXT NOT NULL,
    generated_for_day TEXT NOT NULL,
    reason_code       TEXT NOT NULL,
    reason            TEXT NOT NULL,
    status            TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    handled_by        TEXT,
    handled_at        TEXT,
    note              TEXT,
    UNIQUE (mentor_id, generated_for_day, reason_code)
);
CREATE TABLE IF NOT EXISTS access_log (
    log_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id    TEXT NOT NULL,
    actor_role  TEXT NOT NULL,
    mentor_id   TEXT NOT NULL,
    action      TEXT NOT NULL,
    allowed     INTEGER NOT NULL,
    at_time     TEXT NOT NULL
);
"""


class _LockedConnection:
    """串行化共享连接访问，供多线程 HTTP 服务安全复用。"""

    def __init__(self, path: str | Path) -> None:
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.RLock()

    def execute(self, *args, **kwargs):
        with self._lock:
            return self._conn.execute(*args, **kwargs)

    def executescript(self, *args, **kwargs):
        with self._lock:
            return self._conn.executescript(*args, **kwargs)

    def commit(self) -> None:
        with self._lock:
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class Store:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self.conn = _LockedConnection(path)
        raw = self.conn._conn
        raw.row_factory = sqlite3.Row
        raw.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ---- 用户 ----
    def upsert_user(self, user_id: str, role: str, name: str) -> None:
        self.conn.execute(
            "INSERT INTO users(user_id, role, name) VALUES(?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET role=excluded.role, name=excluded.name",
            (user_id, role, name),
        )
        self.conn.commit()

    def get_user(self, user_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM users WHERE user_id=?", (user_id,)
        ).fetchone()

    # ---- 传承人 ----
    def add_mentor(self, mentor: Mentor) -> None:
        self.conn.execute(
            "INSERT INTO mentors(mentor_id, name, id_digest, phone, created_at) "
            "VALUES(?,?,?,?,?)",
            (mentor.mentor_id, mentor.name, mentor.id_digest, mentor.phone,
             instant_iso(mentor.created_at)),
        )
        self.conn.commit()

    def get_mentor(self, mentor_id: str) -> Mentor | None:
        row = self.conn.execute(
            "SELECT * FROM mentors WHERE mentor_id=?", (mentor_id,)
        ).fetchone()
        return self._mentor(row) if row else None

    def list_mentors(self) -> list[Mentor]:
        rows = self.conn.execute("SELECT * FROM mentors ORDER BY mentor_id").fetchall()
        return [self._mentor(r) for r in rows]

    @staticmethod
    def _mentor(row: sqlite3.Row) -> Mentor:
        return Mentor(
            mentor_id=row["mentor_id"], name=row["name"],
            id_digest=row["id_digest"], phone=row["phone"],
            created_at=parse_instant(row["created_at"]),
        )

    # ---- 资质版本（只追加） ----
    def next_version(self, mentor_id: str) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(MAX(version),0) AS v FROM qualification_versions WHERE mentor_id=?",
            (mentor_id,),
        ).fetchone()
        return int(row["v"]) + 1

    def add_version(self, ver: QualificationVersion) -> None:
        self.conn.execute(
            "INSERT INTO qualification_versions("
            "mentor_id, version, specialties, age_min, age_max, training_records, "
            "cert_source, cert_no, valid_from, valid_until, created_at, author) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                ver.mentor_id, ver.version, json.dumps(list(ver.specialties), ensure_ascii=False),
                ver.age_range[0], ver.age_range[1],
                json.dumps(list(ver.training_records), ensure_ascii=False),
                ver.cert_source, ver.cert_no, d(ver.valid_from), d(ver.valid_until),
                instant_iso(ver.created_at), ver.author,
            ),
        )
        self.conn.commit()

    def list_versions(self, mentor_id: str) -> list[QualificationVersion]:
        rows = self.conn.execute(
            "SELECT * FROM qualification_versions WHERE mentor_id=? ORDER BY version",
            (mentor_id,),
        ).fetchall()
        return [self._version(r) for r in rows]

    def all_versions(self) -> dict[str, list[QualificationVersion]]:
        out: dict[str, list[QualificationVersion]] = {}
        rows = self.conn.execute(
            "SELECT * FROM qualification_versions ORDER BY mentor_id, version"
        ).fetchall()
        for row in rows:
            out.setdefault(row["mentor_id"], []).append(self._version(row))
        return out

    @staticmethod
    def _version(row: sqlite3.Row) -> QualificationVersion:
        return QualificationVersion(
            mentor_id=row["mentor_id"], version=row["version"],
            specialties=tuple(json.loads(row["specialties"])),
            age_range=(row["age_min"], row["age_max"]),
            training_records=tuple(json.loads(row["training_records"])),
            cert_source=row["cert_source"], cert_no=row["cert_no"],
            valid_from=parse_date(row["valid_from"]),
            valid_until=parse_date(row["valid_until"]),
            created_at=parse_instant(row["created_at"]),
            author=row["author"],
        )

    # ---- 区间动作 ----
    def add_interval(self, iv: Interval) -> Interval:
        cur = self.conn.execute(
            "INSERT INTO intervals(mentor_id, kind, start_day, end_day, reason, created_at, author) "
            "VALUES(?,?,?,?,?,?,?)",
            (iv.mentor_id, iv.kind, d(iv.start), d(iv.end), iv.reason,
             instant_iso(iv.created_at), iv.author),
        )
        self.conn.commit()
        return Interval(
            interval_id=cur.lastrowid, mentor_id=iv.mentor_id, kind=iv.kind,
            start=iv.start, end=iv.end, reason=iv.reason,
            created_at=iv.created_at, author=iv.author,
        )

    def list_intervals(self, mentor_id: str) -> list[Interval]:
        rows = self.conn.execute(
            "SELECT * FROM intervals WHERE mentor_id=? ORDER BY interval_id", (mentor_id,)
        ).fetchall()
        return [self._interval(r) for r in rows]

    def all_intervals(self) -> dict[str, list[Interval]]:
        out: dict[str, list[Interval]] = {}
        rows = self.conn.execute("SELECT * FROM intervals ORDER BY mentor_id, interval_id").fetchall()
        for row in rows:
            out.setdefault(row["mentor_id"], []).append(self._interval(row))
        return out

    @staticmethod
    def _interval(row: sqlite3.Row) -> Interval:
        return Interval(
            interval_id=row["interval_id"], mentor_id=row["mentor_id"], kind=row["kind"],
            start=parse_date(row["start_day"]), end=parse_date(row["end_day"]),
            reason=row["reason"], created_at=parse_instant(row["created_at"]),
            author=row["author"],
        )

    # ---- 核验快照（预约/签到，写入后不改） ----
    def add_snapshot(self, snap: VerificationSnapshot) -> VerificationSnapshot:
        cur = self.conn.execute(
            "INSERT INTO snapshots("
            "scope, mentor_id, specialty, audience_age, evaluated_at, effective_day, "
            "decision, reasons, version, intervals, created_by) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                snap.scope, snap.mentor_id, snap.specialty, snap.audience_age,
                instant_iso(snap.evaluated_at), d(snap.effective_day),
                snap.decision, json.dumps(list(snap.reasons), ensure_ascii=False),
                snap.version,
                json.dumps([iv.to_dict() for iv in snap.active_intervals], ensure_ascii=False),
                snap.created_by,
            ),
        )
        self.conn.commit()
        return self._snapshot_by_id(cur.lastrowid)

    def get_snapshot(self, snapshot_id: int) -> VerificationSnapshot | None:
        row = self.conn.execute(
            "SELECT * FROM snapshots WHERE snapshot_id=?", (snapshot_id,)
        ).fetchone()
        return self._snapshot(row) if row else None

    def list_snapshots(self, mentor_id: str | None = None) -> list[VerificationSnapshot]:
        if mentor_id is None:
            rows = self.conn.execute("SELECT * FROM snapshots ORDER BY snapshot_id").fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM snapshots WHERE mentor_id=? ORDER BY snapshot_id", (mentor_id,)
            ).fetchall()
        return [self._snapshot(r) for r in rows]

    def _snapshot_by_id(self, snapshot_id: int) -> VerificationSnapshot:
        row = self.conn.execute(
            "SELECT * FROM snapshots WHERE snapshot_id=?", (snapshot_id,)
        ).fetchone()
        return self._snapshot(row)

    @staticmethod
    def _snapshot(row: sqlite3.Row) -> VerificationSnapshot:
        ivs = [
            Interval(
                interval_id=item["interval_id"], mentor_id=item["mentor_id"],
                kind=item["kind"], start=parse_date(item["start"]),
                end=parse_date(item["end"]), reason=item["reason"],
                created_at=parse_instant(item["created_at"]), author=item["author"],
            )
            for item in json.loads(row["intervals"])
        ]
        return VerificationSnapshot(
            snapshot_id=row["snapshot_id"], scope=row["scope"],
            mentor_id=row["mentor_id"], specialty=row["specialty"],
            audience_age=row["audience_age"],
            evaluated_at=parse_instant(row["evaluated_at"]),
            effective_day=parse_date(row["effective_day"]),
            decision=row["decision"], reasons=tuple(json.loads(row["reasons"])),
            version=row["version"], active_intervals=tuple(ivs),
            created_by=row["created_by"],
        )

    # ---- 巡检案件 ----
    def case_exists(self, mentor_id: str, day: date, reason_code: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM cases WHERE mentor_id=? AND generated_for_day=? AND reason_code=?",
            (mentor_id, d(day), reason_code),
        ).fetchone()
        return row is not None

    def add_case(self, case: Case) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO cases("
            "mentor_id, generated_for_day, reason_code, reason, status, created_at) "
            "VALUES(?,?,?,?,?,?)",
            (case.mentor_id, d(case.generated_for_day), case.reason_code,
             case.reason, case.status, instant_iso(case.created_at)),
        )
        self.conn.commit()

    def list_cases(self, status: str | None = None) -> list[Case]:
        if status is None:
            rows = self.conn.execute("SELECT * FROM cases ORDER BY case_id").fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM cases WHERE status=? ORDER BY case_id", (status,)
            ).fetchall()
        return [self._case(r) for r in rows]

    def get_case(self, case_id: int) -> Case | None:
        row = self.conn.execute("SELECT * FROM cases WHERE case_id=?", (case_id,)).fetchone()
        return self._case(row) if row else None

    def resolve_case(self, case_id: int, handled_by: str, note: str, at: datetime) -> None:
        self.conn.execute(
            "UPDATE cases SET status='resolved', handled_by=?, handled_at=?, note=? "
            "WHERE case_id=?",
            (handled_by, instant_iso(at), note, case_id),
        )
        self.conn.commit()

    @staticmethod
    def _case(row: sqlite3.Row) -> Case:
        return Case(
            case_id=row["case_id"], mentor_id=row["mentor_id"],
            generated_for_day=parse_date(row["generated_for_day"]),
            reason_code=row["reason_code"], reason=row["reason"],
            status=row["status"], created_at=parse_instant(row["created_at"]),
            handled_by=row["handled_by"],
            handled_at=parse_instant(row["handled_at"]) if row["handled_at"] else None,
            note=row["note"],
        )

    # ---- 访问留痕 ----
    def log_access(self, actor_id: str, actor_role: str, mentor_id: str,
                   action: str, allowed: bool, at: datetime) -> None:
        self.conn.execute(
            "INSERT INTO access_log(actor_id, actor_role, mentor_id, action, allowed, at_time) "
            "VALUES(?,?,?,?,?,?)",
            (actor_id, actor_role, mentor_id, action, 1 if allowed else 0, instant_iso(at)),
        )
        self.conn.commit()

    def list_access_log(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM access_log ORDER BY log_id").fetchall()
        return [dict(r) for r in rows]
