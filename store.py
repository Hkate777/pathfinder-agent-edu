"""SQLite persistence: append-only mistakes, recomputed gap profiles, practice log."""

from __future__ import annotations

import os
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS students (
    id TEXT PRIMARY KEY,
    grade TEXT,
    class_id TEXT
);
CREATE TABLE IF NOT EXISTS mistakes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    image_path TEXT,
    question_text TEXT,
    steps TEXT,
    broken_step TEXT,
    error_type TEXT,
    knowledge_point_id TEXT,
    confidence REAL,
    grounded INTEGER,
    provider TEXT
);
CREATE TABLE IF NOT EXISTS practice_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mistake_id INTEGER NOT NULL,
    item_id TEXT,
    due_day INTEGER,
    result TEXT,
    logged_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mistake_details (
    mistake_id INTEGER PRIMARY KEY REFERENCES mistakes(id),
    explanation TEXT,
    grounding_score REAL,
    needs_review INTEGER NOT NULL,
    source TEXT
);
CREATE TABLE IF NOT EXISTS teacher_reviews (
    mistake_id INTEGER PRIMARY KEY REFERENCES mistakes(id),
    decision TEXT NOT NULL CHECK(decision IN ('confirmed', 'rejected')),
    note TEXT NOT NULL,
    reviewed_at TEXT NOT NULL
);
-- A teacher may assign the syllabus knowledge point when automatic grounding
-- stayed below threshold. This is the only way an "unmapped" case reaches practice.
CREATE TABLE IF NOT EXISTS teacher_kp_override (
    mistake_id INTEGER PRIMARY KEY REFERENCES mistakes(id),
    knowledge_point_id TEXT NOT NULL,
    decided_at TEXT NOT NULL
);
-- Idempotency: one submission (student + input) maps to exactly one stored
-- mistake, so a browser refresh cannot create a duplicate row.
CREATE TABLE IF NOT EXISTS submissions (
    submission_key TEXT PRIMARY KEY,
    mistake_id INTEGER NOT NULL REFERENCES mistakes(id),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS mistakes_student ON mistakes(student_id, id);
CREATE INDEX IF NOT EXISTS practice_by_mistake ON practice_log(mistake_id, item_id, result);
"""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_timestamp(value: str) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def connect(db_path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def add_mistake(
    conn: sqlite3.Connection, record: Dict[str, Any], *,
    details: Optional[Dict[str, Any]] = None, commit: bool = True,
) -> int:
    cur = conn.execute(
        """INSERT INTO mistakes
           (student_id, created_at, image_path, question_text, steps, broken_step,
            error_type, knowledge_point_id, confidence, grounded, provider)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (
            record.get("student_id", "anon"),
            record.get("created_at") or utc_now().isoformat(timespec="seconds"),
            record.get("image_path"),
            record.get("question_text", ""),
            "\n".join(record.get("steps", []) or []),
            record.get("broken_step"),
            record.get("error_type"),
            record.get("knowledge_point_id"),
            record.get("confidence"),
            1 if record.get("grounded") else 0,
            record.get("provider", ""),
        ),
    )
    mistake_id = int(cur.lastrowid)
    if details is not None:
        conn.execute(
            """INSERT INTO mistake_details (mistake_id, explanation, grounding_score, needs_review, source)
               VALUES (?, ?, ?, ?, ?)""",
            (mistake_id, details.get("explanation"), details.get("grounding_score"),
             int(details["needs_review"]), details.get("source")),
        )
    if commit:
        conn.commit()
    return mistake_id


def find_submission(conn: sqlite3.Connection, submission_key: str) -> Optional[int]:
    """Return the mistake id already stored for this submission, if any."""
    row = conn.execute(
        "SELECT mistake_id FROM submissions WHERE submission_key=?", (submission_key,)
    ).fetchone()
    return int(row["mistake_id"]) if row else None


def record_submission(conn: sqlite3.Connection, submission_key: str, mistake_id: int) -> None:
    conn.execute(
        "INSERT INTO submissions (submission_key, mistake_id, created_at) VALUES (?, ?, ?)",
        (submission_key, mistake_id, utc_now().isoformat(timespec="seconds")),
    )


def save_kp_override(conn: sqlite3.Connection, mistake_id: int, knowledge_point_id: str) -> bool:
    """Bind a teacher-assigned knowledge point to an unmapped mistake."""
    if not knowledge_point_id or not knowledge_point_id.strip():
        raise ValueError("Knowledge point override must be a non-empty id.")
    cur = conn.execute(
        """INSERT INTO teacher_kp_override (mistake_id, knowledge_point_id, decided_at)
           VALUES (?, ?, ?)
           ON CONFLICT(mistake_id) DO UPDATE SET
             knowledge_point_id=excluded.knowledge_point_id, decided_at=excluded.decided_at""",
        (mistake_id, knowledge_point_id.strip(), utc_now().isoformat(timespec="seconds")),
    )
    conn.commit()
    return cur.rowcount == 1


def effective_kp(conn: sqlite3.Connection, mistake_id: int) -> Optional[str]:
    row = conn.execute(
        """SELECT COALESCE(o.knowledge_point_id, m.knowledge_point_id) AS kp
           FROM mistakes m LEFT JOIN teacher_kp_override o ON o.mistake_id=m.id
           WHERE m.id=?""",
        (mistake_id,),
    ).fetchone()
    return row["kp"] if row else None


def record_practice(
    conn: sqlite3.Connection, mistake_id: int, item_id: str, due_day: int, result: str, *,
    max_per_day: Optional[int] = None, not_before: Optional[date] = None,
) -> bool:
    now = utc_now()
    today = now.astimezone().date()
    if not_before and today < not_before:
        return False
    with conn:
        if max_per_day is not None:
            # Reserve the write before checking the limit; browser sessions can submit concurrently.
            conn.execute("BEGIN IMMEDIATE")
            student = conn.execute("SELECT student_id FROM mistakes WHERE id=?", (mistake_id,)).fetchone()
            if student is None:
                return False
            studied = practice_on(conn, student["student_id"], today)
            if len(studied) >= max_per_day and (mistake_id, item_id) not in studied:
                return False
        cur = conn.execute(
            """INSERT INTO practice_log (mistake_id, item_id, due_day, result, logged_at)
               SELECT ?, ?, ?, ?, ?
               WHERE EXISTS (SELECT 1 FROM mistakes WHERE id=?)
                 AND NOT EXISTS (
                   SELECT 1 FROM practice_log WHERE mistake_id=? AND item_id=? AND result='pass'
                 )""",
            (mistake_id, item_id, due_day, result, now.isoformat(timespec="seconds"),
             mistake_id, mistake_id, item_id),
        )
    return cur.rowcount == 1


def practice_on(conn: sqlite3.Connection, student_id: str, day: date) -> set[tuple[int, str]]:
    return {
        (row["mistake_id"], row["item_id"]) for row in conn.execute(
            """SELECT DISTINCT p.mistake_id, p.item_id FROM practice_log p
               JOIN mistakes m ON m.id=p.mistake_id WHERE m.student_id=?
               AND date(p.logged_at, 'localtime')=?""",
            (student_id, day.isoformat()),
        )
    }


def completed_practice(conn: sqlite3.Connection, student_id: str) -> Dict[int, set[str]]:
    completed: Dict[int, set[str]] = {}
    for row in conn.execute(
        """SELECT DISTINCT p.mistake_id, p.item_id FROM practice_log p
           JOIN mistakes m ON m.id=p.mistake_id WHERE m.student_id=? AND p.result='pass'""",
        (student_id,),
    ):
        completed.setdefault(row["mistake_id"], set()).add(row["item_id"])
    return completed


def register_student(conn: sqlite3.Connection, student_id: str, class_id: Optional[str] = None) -> None:
    conn.execute(
        """INSERT INTO students (id, class_id) VALUES (?, ?)
           ON CONFLICT(id) DO UPDATE SET class_id=excluded.class_id""",
        (student_id, class_id),
    )
    conn.commit()


def list_classes(conn: sqlite3.Connection) -> List[str]:
    return [row[0] for row in conn.execute(
        "SELECT DISTINCT class_id FROM students WHERE class_id IS NOT NULL ORDER BY class_id"
    )]


DETAILS = """
SELECT m.*, s.class_id, d.explanation, d.grounding_score, d.source,
       COALESCE(o.knowledge_point_id, m.knowledge_point_id) AS effective_kp,
       o.knowledge_point_id AS override_kp,
       COALESCE(d.needs_review,
           m.confidence IS NULL OR m.confidence < ?
           OR COALESCE(TRIM(m.broken_step), '')=''
           OR instr(char(10) || COALESCE(m.steps, '') || char(10),
                    char(10) || m.broken_step || char(10))=0
       ) AS needs_review,
       r.decision, r.note AS review_note, r.reviewed_at
FROM mistakes m
LEFT JOIN students s ON s.id=m.student_id
LEFT JOIN mistake_details d ON d.mistake_id=m.id
LEFT JOIN teacher_reviews r ON r.mistake_id=m.id
LEFT JOIN teacher_kp_override o ON o.mistake_id=m.id
"""


def mistake_history(conn: sqlite3.Connection, student_id: str, threshold: float) -> List[sqlite3.Row]:
    return conn.execute(
        f"{DETAILS} WHERE m.student_id=? ORDER BY m.id DESC", (threshold, student_id),
    ).fetchall()


def mistake_details(conn: sqlite3.Connection, mistake_id: int, threshold: float) -> Optional[sqlite3.Row]:
    return conn.execute(f"{DETAILS} WHERE m.id=?", (threshold, mistake_id)).fetchone()


def review_cases(
    conn: sqlite3.Connection, threshold: float, class_id: Optional[str] = None, *, resolved: bool = False,
) -> List[sqlite3.Row]:
    return conn.execute(
        f"""SELECT * FROM ({DETAILS})
            WHERE (? IS NULL OR class_id=?)
              AND (needs_review OR COALESCE(grounded, 0)=0)
              AND (decision IS NOT NULL)=?
            ORDER BY id DESC""",
        (threshold, class_id, class_id, int(resolved)),
    ).fetchall()


def save_review(conn: sqlite3.Connection, mistake_id: int, decision: str, note: str) -> bool:
    if decision not in ("confirmed", "rejected") or not note.strip():
        raise ValueError("Review requires a decision and a note.")
    cur = conn.execute(
        """INSERT INTO teacher_reviews (mistake_id, decision, note, reviewed_at)
           VALUES (?, ?, ?, ?) ON CONFLICT(mistake_id) DO NOTHING""",
        (mistake_id, decision, note.strip(), utc_now().isoformat(timespec="seconds")),
    )
    conn.commit()
    return cur.rowcount == 1


def list_mistakes(conn: sqlite3.Connection, student_id: Optional[str] = None, limit: int = 100) -> List[sqlite3.Row]:
    if student_id:
        rows = conn.execute(
            "SELECT * FROM mistakes WHERE student_id=? ORDER BY id DESC LIMIT ?", (student_id, limit)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM mistakes ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return rows


def gap_profile(conn: sqlite3.Connection, student_id: str) -> List[Dict[str, Any]]:
    """Recomputed from append-only mistakes; never mutated in place."""
    rows = conn.execute(
        """SELECT COALESCE(o.knowledge_point_id, m.knowledge_point_id) AS knowledge_point_id,
                  m.error_type, m.created_at FROM mistakes m
           LEFT JOIN teacher_reviews r ON r.mistake_id=m.id
           LEFT JOIN teacher_kp_override o ON o.mistake_id=m.id
           WHERE m.student_id=? AND COALESCE(o.knowledge_point_id, m.knowledge_point_id) IS NOT NULL
             AND r.decision IS NOT 'rejected'
           ORDER BY m.id DESC""",
        (student_id,),
    ).fetchall()
    now = utc_now()
    profile: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        kp = row["knowledge_point_id"]
        seen = parse_timestamp(row["created_at"]) or now
        entry = profile.setdefault(
            kp, {"knowledge_point": kp, "repeat_count": 0, "last_seen": seen, "error_type": row["error_type"]}
        )
        entry["repeat_count"] += 1
        if seen > entry["last_seen"]:
            entry["last_seen"] = seen
            entry["error_type"] = row["error_type"]  # latest mistake defines current severity
    for entry in profile.values():
        cleared = conn.execute(
            """SELECT COUNT(DISTINCT p.mistake_id || ':' || p.item_id) c
               FROM practice_log p JOIN mistakes m ON m.id = p.mistake_id
               LEFT JOIN teacher_kp_override o ON o.mistake_id=m.id
               WHERE m.student_id=? AND COALESCE(o.knowledge_point_id, m.knowledge_point_id)=?
                 AND p.result='pass'""",
            (student_id, entry["knowledge_point"]),
        ).fetchone()["c"]
        entry["cleared_count"] = cleared
        entry["days_since"] = max(0, (now - entry["last_seen"]).days)
        entry["last_seen"] = entry["last_seen"].isoformat(timespec="seconds")
    return sorted(profile.values(), key=lambda e: -e["repeat_count"])


def repeat_error_rate(conn: sqlite3.Connection, student_id: Optional[str] = None, window_days: int = 14) -> Dict[str, Any]:
    since = (utc_now() - timedelta(days=window_days)).isoformat(timespec="seconds")
    params: List[Any] = [since]
    where = "WHERE julianday(m.created_at) >= julianday(?) AND r.decision IS NOT 'rejected'"
    if student_id:
        where += " AND m.student_id=?"
        params.append(student_id)
    rows = conn.execute(
        f"""SELECT COALESCE(o.knowledge_point_id, m.knowledge_point_id) kp, COUNT(*) c FROM mistakes m
             LEFT JOIN teacher_reviews r ON r.mistake_id=m.id
             LEFT JOIN teacher_kp_override o ON o.mistake_id=m.id
             {where} AND COALESCE(o.knowledge_point_id, m.knowledge_point_id) IS NOT NULL
             GROUP BY COALESCE(o.knowledge_point_id, m.knowledge_point_id)""",
        params,
    ).fetchall()
    total = sum(r["c"] for r in rows)
    repeat_points = sum(1 for r in rows if r["c"] > 1)
    points = len(rows)
    return {
        "window_days": window_days,
        "distinct_knowledge_points": points,
        "repeat_knowledge_points": repeat_points,
        "repeat_error_rate": round(repeat_points / points, 4) if points else 0.0,
        "observations": total,
    }


def class_heatmap(
    conn: sqlite3.Connection, class_id: Optional[str] = None, *, by_error: bool = False,
) -> List[Dict[str, Any]]:
    kp = "COALESCE(o.knowledge_point_id, m.knowledge_point_id)"
    conditions = [f"{kp} IS NOT NULL", "r.decision IS NOT 'rejected'"]
    params: List[Any] = []
    if class_id:
        conditions.append("s.class_id=?")
        params.append(class_id)
    rows = conn.execute(
        f"""SELECT {kp} kp, m.error_type, COUNT(*) c
            FROM mistakes m LEFT JOIN students s ON s.id = m.student_id
            LEFT JOIN teacher_reviews r ON r.mistake_id=m.id
            LEFT JOIN teacher_kp_override o ON o.mistake_id=m.id
            WHERE {' AND '.join(conditions)}
            GROUP BY {kp}, m.error_type""",
        params,
    ).fetchall()
    counts: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        entry = counts.setdefault(row["kp"], {"knowledge_point": row["kp"], "mistakes": 0, "error_types": {}})
        entry["mistakes"] += row["c"]
        entry["error_types"][row["error_type"]] = row["c"]
    result = sorted(counts.values(), key=lambda entry: (-entry["mistakes"], entry["knowledge_point"]))
    if not by_error:
        for entry in result:
            del entry["error_types"]
    return result
