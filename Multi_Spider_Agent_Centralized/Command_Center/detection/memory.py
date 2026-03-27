"""
Environment Memory: stores detection history from all agents.

Tracks what was detected, where, when, and by which agent.
Provides query APIs for the planner and frontend.
"""

import json
import logging
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent / "detection_history.db"


class DetectionMemory:
    """SQLite-backed detection history store."""

    def __init__(self, db_path: str = str(DB_PATH)):
        self._db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS detections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    task_type TEXT NOT NULL,
                    target_type TEXT,
                    label TEXT,
                    confidence REAL,
                    x_cm REAL,
                    y_cm REAL,
                    heading_deg REAL,
                    llm_match INTEGER DEFAULT 0,
                    raw_json TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS missions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    prompt TEXT,
                    task_type TEXT,
                    status TEXT,
                    duration_ms INTEGER,
                    detection_count INTEGER DEFAULT 0,
                    summary TEXT
                )
            """)
            conn.commit()
            conn.close()

    def record_detections(
        self,
        agent_id: str,
        task_type: str,
        target_type: str,
        detections: list[dict],
    ):
        """Store detection results from an agent response."""
        now = datetime.utcnow().isoformat()
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            for d in detections:
                conn.execute(
                    """INSERT INTO detections
                       (timestamp, agent_id, task_type, target_type, label,
                        confidence, x_cm, y_cm, heading_deg, llm_match, raw_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        now,
                        agent_id,
                        task_type,
                        target_type,
                        d.get("label", ""),
                        d.get("confidence", 0),
                        d.get("x_cm", 0),
                        d.get("y_cm", 0),
                        d.get("heading_deg", 0),
                        1 if d.get("llm_match") else 0,
                        json.dumps(d),
                    ),
                )
            conn.commit()
            conn.close()
        logger.info(f"Memory: recorded {len(detections)} detections from {agent_id}")

    def record_mission(
        self,
        task_id: str,
        agent_id: str,
        prompt: str,
        task_type: str,
        status: str,
        duration_ms: int = 0,
        detection_count: int = 0,
        summary: str = "",
    ):
        """Store a completed mission record."""
        now = datetime.utcnow().isoformat()
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            conn.execute(
                """INSERT INTO missions
                   (timestamp, task_id, agent_id, prompt, task_type,
                    status, duration_ms, detection_count, summary)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (now, task_id, agent_id, prompt, task_type,
                 status, duration_ms, detection_count, summary),
            )
            conn.commit()
            conn.close()

    def get_recent_detections(self, limit: int = 50, target_type: Optional[str] = None) -> list[dict]:
        """Get recent detections, optionally filtered by target type."""
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            conn.row_factory = sqlite3.Row
            if target_type:
                rows = conn.execute(
                    "SELECT * FROM detections WHERE target_type = ? ORDER BY timestamp DESC LIMIT ?",
                    (target_type, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM detections ORDER BY timestamp DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            conn.close()
        return [dict(r) for r in rows]

    def get_recent_missions(self, limit: int = 20) -> list[dict]:
        """Get recent mission records."""
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM missions ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            ).fetchall()
            conn.close()
        return [dict(r) for r in rows]

    def get_detection_summary(self) -> dict:
        """Get summary statistics of all detections."""
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            total = conn.execute("SELECT COUNT(*) FROM detections").fetchone()[0]
            by_type = conn.execute(
                "SELECT target_type, COUNT(*) as cnt FROM detections GROUP BY target_type"
            ).fetchall()
            by_agent = conn.execute(
                "SELECT agent_id, COUNT(*) as cnt FROM detections GROUP BY agent_id"
            ).fetchall()
            recent = conn.execute(
                "SELECT * FROM detections ORDER BY timestamp DESC LIMIT 1"
            ).fetchone()
            mission_count = conn.execute("SELECT COUNT(*) FROM missions").fetchone()[0]
            conn.close()

        return {
            "total_detections": total,
            "total_missions": mission_count,
            "by_type": {row[0]: row[1] for row in by_type},
            "by_agent": {row[0]: row[1] for row in by_type},
            "last_detection": dict(sqlite3.Row(recent)) if recent else None,
        }

    def clear(self):
        """Clear all history."""
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            conn.execute("DELETE FROM detections")
            conn.execute("DELETE FROM missions")
            conn.commit()
            conn.close()
        logger.info("Memory: cleared all history")


# Singleton instance
detection_memory = DetectionMemory()
