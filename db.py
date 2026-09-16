"""
db.py
-----
Tiny SQLite layer for the detection / verification queue, plus the
Track B corroboration + authority-routing surfaces (all additive).

Track A (detection -> admin verify -> letter PDF) is untouched: the
`detections` table keeps every existing column and workflow.

Track B adds, without touching Track A:
- `detections.client_ip`            - citizen's corroboration identity (server
                                     captured from request hop X-Forwarded-For /
                                     remote_addr; NEVER a form field)
- `detections.hazard_type`          - default 'road_damage'; groups corroboration
                                     clusters and authority routing
- `detections.corroboration_area_key` - fine grid key (~111 m cells), one per
                                     citizen report; corroboration clusters group
                                     on it
- `detections.authority_area_key`   - coarse grid key (~1.1 km cells), one per
                                     report; correlates to the authority that
                                     owns that jurisdiction
- `authorities`                     - one row per typed/verified authority email
                                     for an area + hazard: lifecycle
                                     pending -> verified, or verified -> bounced
                                     -> pending (flip, not a new tier)
- `corroboration_clusters`          - corroboration state per (fine area,
                                     hazard_type, window). Count is ALWAYS derived
                                     (COUNT(DISTINCT client_ip)) on demand — no
                                     stored counter, no stored count column.
"""

import sqlite3
import os
from datetime import datetime, timezone

DB_PATH = os.path.join(os.path.dirname(__file__), "smartsurround.db")


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _has_column(conn, table, column):
    cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    return column in cols


def _add_column(conn, table, ddl):
    if not _has_column(conn, table, ddl.split()[0]):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def init_db():
    conn = get_conn()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS detections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            image_path TEXT NOT NULL,
            source TEXT NOT NULL,            -- 'esp32' or 'citizen'
            damage_class TEXT,
            confidence REAL,
            severity TEXT,                   -- Normal / Warning / Critical
            lat REAL,
            lon REAL,
            description TEXT,                -- optional citizen-provided note
            status TEXT NOT NULL DEFAULT 'pending',  -- pending/approved/rejected/letter_sent
            ai_accepted INTEGER,             -- 1 if the model's confidence cleared
                                             -- ACCEPT_THRESHOLD (detector.py's
                                             -- conservative decision layer), else 0
            created_at TEXT NOT NULL,
            reviewed_at TEXT,
            letter_path TEXT
        )
        """
    )

    # --- Track B (additive, guarded so existing DBs migrate, never break) ---
    _add_column(conn, "detections",
                "client_ip TEXT")                     # server-captured corroboration identity
    _add_column(conn, "detections",
                "hazard_type TEXT DEFAULT 'road_damage'")
    _add_column(conn, "detections",
                "corroboration_area_key TEXT")        # fine grid key (~111 m cells)
    _add_column(conn, "detections",
                "authority_area_key TEXT")            # coarse grid key (~1.1 km cells)

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS authorities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            authority_area_key TEXT NOT NULL,
            hazard_type TEXT NOT NULL,
            email TEXT NOT NULL,
            lifecycle TEXT NOT NULL DEFAULT 'pending',  -- pending/verified/bounced
            created_at TEXT NOT NULL,
            verified_at TEXT,
            bounced_at TEXT,
            bounce_reason TEXT,
            UNIQUE(authority_area_key, hazard_type, email)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS corroboration_clusters (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            corroboration_area_key TEXT NOT NULL,
            hazard_type TEXT NOT NULL,
            window_start TEXT NOT NULL,
            lifecycle TEXT NOT NULL DEFAULT 'open',     -- open/corroborated/emailed/settled
            letter_path TEXT,
            emailed_at TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(corroboration_area_key, hazard_type, window_start)
        )
        """
    )

    conn.commit()
    conn.close()


def insert_detection(image_path, source, damage_class, confidence, severity,
                     lat=None, lon=None, description=None, ai_accepted=None,
                     client_ip=None, hazard_type=None,
                     corroboration_area_key=None, authority_area_key=None):
    conn = get_conn()
    cur = conn.execute(
        """
        INSERT INTO detections
            (image_path, source, damage_class, confidence, severity,
             lat, lon, description, ai_accepted, status, created_at,
             client_ip, hazard_type, corroboration_area_key, authority_area_key)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)
        """,
        (
            image_path, source, damage_class, confidence, severity,
            lat, lon, description,
            int(ai_accepted) if ai_accepted is not None else None,
            datetime.now(timezone.utc).isoformat(),
            client_ip, hazard_type or "road_damage",
            corroboration_area_key, authority_area_key,
        ),
    )
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return new_id


def list_by_status(status):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM detections WHERE status = ? ORDER BY created_at DESC", (status,)
    ).fetchall()
    conn.close()
    return rows


def get_detection(detection_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM detections WHERE id = ?", (detection_id,)).fetchone()
    conn.close()
    return row


def update_status(detection_id, status, letter_path=None):
    conn = get_conn()
    conn.execute(
        """
        UPDATE detections
        SET status = ?, reviewed_at = ?, letter_path = COALESCE(?, letter_path)
        WHERE id = ?
        """,
        (status, datetime.now(timezone.utc).isoformat(), letter_path, detection_id),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Track B — authorities (lifecycle pending -> verified, verified -> bounced
# -> pending; a flip, not a new tier)
# ---------------------------------------------------------------------------

def get_or_create_authority(authority_area_key, hazard_type, email):
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM authorities WHERE authority_area_key = ? AND hazard_type = ? AND email = ?",
        (authority_area_key, hazard_type, email),
    ).fetchone()
    if row is None:
        cur = conn.execute(
            """
            INSERT INTO authorities (authority_area_key, hazard_type, email, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (authority_area_key, hazard_type, email, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        authority_id = cur.lastrowid
        conn.close()
        return get_authority(authority_id)
    conn.close()
    return row


def get_authority(authority_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM authorities WHERE id = ?", (authority_id,)).fetchone()
    conn.close()
    return row


def get_verified_authority(authority_area_key, hazard_type):
    conn = get_conn()
    row = conn.execute(
        """
        SELECT * FROM authorities
        WHERE authority_area_key = ? AND hazard_type = ? AND lifecycle = 'verified'
        ORDER BY verified_at ASC LIMIT 1
        """,
        (authority_area_key, hazard_type),
    ).fetchone()
    conn.close()
    return row


def list_authorities():
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM authorities ORDER BY created_at DESC"
    ).fetchall()
    conn.close()
    return rows


def mark_authority_verified(authority_id):
    conn = get_conn()
    conn.execute(
        """
        UPDATE authorities
        SET lifecycle = 'verified', verified_at = ?, bounced_at = NULL, bounce_reason = NULL
        WHERE id = ?
        """,
        (datetime.now(timezone.utc).isoformat(), authority_id),
    )
    conn.commit()
    conn.close()


def mark_authority_bounced(authority_id, reason=None):
    """verified -> bounced -> pending (flip, so a later verified set re-enables)."""
    conn = get_conn()
    conn.execute(
        """
        UPDATE authorities
        SET lifecycle = 'pending', bounced_at = ?, bounce_reason = ?
        WHERE id = ?
        """,
        (datetime.now(timezone.utc).isoformat(), reason, authority_id),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Track B — corroboration clusters (state only; count is ALWAYS derived from
# detections.client_ip on demand, never stored)
# ---------------------------------------------------------------------------

CLUSTER_WINDOW_STEP = 3600  # 1 h window, sweep step (kept here for config surface)


def get_or_create_cluster(corroboration_area_key, hazard_type, window_start):
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM corroboration_clusters WHERE corroboration_area_key = ? AND hazard_type = ? AND window_start = ?",
        (corroboration_area_key, hazard_type, window_start),
    ).fetchone()
    if row is None:
        conn.execute(
            """
            INSERT INTO corroboration_clusters
                (corroboration_area_key, hazard_type, window_start, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (corroboration_area_key, hazard_type, window_start,
             datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        conn.close()
        return get_cluster_by_window(corroboration_area_key, hazard_type, window_start)
    conn.close()
    return row


def get_cluster_by_window(corroboration_area_key, hazard_type, window_start):
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM corroboration_clusters WHERE corroboration_area_key = ? AND hazard_type = ? AND window_start = ?",
        (corroboration_area_key, hazard_type, window_start),
    ).fetchone()
    conn.close()
    return row


def list_clusters():
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM corroboration_clusters ORDER BY created_at DESC"
    ).fetchall()
    conn.close()
    return rows


def update_cluster(cluster_id, lifecycle=None, letter_path=None, emailed_at=None):
    conn = get_conn()
    if lifecycle is not None:
        conn.execute(
            "UPDATE corroboration_clusters SET lifecycle = ? WHERE id = ?",
            (lifecycle, cluster_id),
        )
    if letter_path is not None:
        conn.execute(
            "UPDATE corroboration_clusters SET letter_path = ? WHERE id = ?",
            (letter_path, cluster_id),
        )
    if emailed_at is not None:
        conn.execute(
            "UPDATE corroboration_clusters SET emailed_at = ? WHERE id = ?",
            (emailed_at, cluster_id),
        )
    conn.commit()
    conn.close()


def corroboration_count(conn, corroboration_area_key, hazard_type, window_start, window_end):
    """ALWAYS derived: COUNT(DISTINCT client_ip) in the fine area+hazard window."""
    return conn.execute(
        """
        SELECT COUNT(DISTINCT client_ip)
        FROM detections
        WHERE corroboration_area_key = ?
          AND hazard_type = ?
          AND client_ip IS NOT NULL
          AND created_at >= ? AND created_at < ?
        """,
        (corroboration_area_key, hazard_type, window_start, window_end),
    ).fetchone()[0]
