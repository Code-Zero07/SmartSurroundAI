"""
db.py
-----
Tiny SQLite layer for the detection / verification queue, plus the
geographical clustering + authority-routing surfaces.

The `detections` table keeps every existing column and adds the normalized
hazard fields:
- `hazard_category`          - normalized hazard category (e.g. 'road_damage')
- `hazard_type`              - normalized specific type slug (e.g. 'pothole')
- `location_name`            - citizen-editable display label only
- `authority_area`           - server-derived area label (geocoder, never client)
- `detection_model` / `detection_model_version` - inference provenance
- `cluster_id`               - the corroboration_clusters generation this report joined

`authorities` holds one row per typed/verified authority email for an area +
hazard: lifecycle pending -> verified, or verified -> bounced -> pending
(flip, not a new tier).

`corroboration_clusters` is repurposed as geographical organization for admin
review, NOT corroboration counting: one ACTIVE cluster per (fine area key,
hazard_category) where active = pending_approval/approved/missing_authority_email,
enforced by a partial unique index. `sent`/`settled` are terminal; a later
report after a terminal cluster starts a NEW generation (new id -> new email).
"""

import sqlite3
import os
from datetime import datetime, timezone

import hazard_types

DB_PATH = os.path.join(os.path.dirname(__file__), "smartsurround.db")

CLUSTER_ACTIVE_LIFECYCLES = (
    "pending_approval",
    "approved",
    "missing_authority_email",
)

_CLUSTER_DDL = """
CREATE TABLE IF NOT EXISTS corroboration_clusters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    corroboration_area_key TEXT NOT NULL,
    hazard_category TEXT NOT NULL,
    lifecycle TEXT NOT NULL DEFAULT 'pending_approval',
    representative_report_id INTEGER,
    authority_area_key TEXT,
    letter_path TEXT,
    sent_at TEXT,
    last_send_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

_CLUSTER_ACTIVE_INDEX_DDL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_cluster "
    "ON corroboration_clusters (corroboration_area_key, hazard_category) "
    "WHERE lifecycle IN ('pending_approval','approved','missing_authority_email')"
)


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


def _migrate_hazard_schema(conn):
    # 1) additive columns on detections / authorities
    _add_column(conn, "detections", "hazard_category TEXT NOT NULL DEFAULT 'road_damage'")
    _add_column(conn, "detections", "location_name TEXT")
    _add_column(conn, "detections", "authority_area TEXT")
    _add_column(conn, "detections", "detection_model TEXT")
    _add_column(conn, "detections", "detection_model_version TEXT")
    _add_column(conn, "detections", "cluster_id INTEGER")
    _add_column(conn, "authorities", "authority_name TEXT")

    # 2) backfill hazard_category from the legacy hazard_type VALUE when that
    #    value is not already a normalized type slug (legacy rows carried the
    #    category 'road_damage' in hazard_type).
    conn.execute(
        """
        UPDATE detections
        SET hazard_category = hazard_type
        WHERE hazard_category = 'road_damage'
          AND hazard_type IS NOT NULL
          AND hazard_type NOT IN ('pothole','alligator_crack',
                                  'longitudinal_crack','transverse_crack',
                                  'unclassified_damage')
          AND hazard_type <> 'road_damage'
        """
    )

    # 3) legacy hazard_type rows still holding the category key become the
    #    normalized specific type derived from damage_class.
    rows = conn.execute(
        "SELECT id, damage_class FROM detections WHERE hazard_type = 'road_damage'"
    ).fetchall()
    for r in rows:
        slug = hazard_types.type_slug(r["damage_class"])
        if slug:
            conn.execute(
                "UPDATE detections SET hazard_type = ? WHERE id = ?",
                (slug, r["id"]),
            )

    # 4) corroboration_clusters: rebuild from the legacy windowed schema,
    #    preserving historical rows as terminal generations.
    if _has_column(conn, "corroboration_clusters", "window_start"):
        legacy = conn.execute("SELECT * FROM corroboration_clusters").fetchall()
        conn.execute("DROP TABLE corroboration_clusters")
        conn.execute(_CLUSTER_DDL)
        conn.execute(_CLUSTER_ACTIVE_INDEX_DDL)
        legacy_map = {
            "open": "pending_approval",
            "corroborated": "pending_approval",
            "emailed": "sent",
            "settled": "settled",
        }
        for r in legacy:
            lifecycle = legacy_map.get(r["lifecycle"], "pending_approval")
            sent_at = r["emailed_at"] if lifecycle == "sent" else None
            conn.execute(
                """
                INSERT INTO corroboration_clusters
                    (corroboration_area_key, hazard_category, lifecycle,
                     letter_path, sent_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (r["corroboration_area_key"], r["hazard_type"], lifecycle,
                 r["letter_path"], sent_at, r["created_at"], r["created_at"]),
            )
    else:
        conn.execute(_CLUSTER_DDL)
        conn.execute(_CLUSTER_ACTIVE_INDEX_DDL)


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
            status TEXT NOT NULL DEFAULT 'pending',  -- draft/pending/approved/rejected
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

    _migrate_hazard_schema(conn)

    conn.commit()
    conn.close()


def insert_detection(image_path, source, damage_class, confidence, severity,
                     lat=None, lon=None, description=None, ai_accepted=None,
                     client_ip=None, hazard_type=None, hazard_category=None,
                     location_name=None, authority_area=None,
                     detection_model=None, detection_model_version=None,
                     corroboration_area_key=None, authority_area_key=None,
                     status="pending"):
    conn = get_conn()
    cur = conn.execute(
        """
        INSERT INTO detections
            (image_path, source, damage_class, confidence, severity,
             lat, lon, description, ai_accepted, status, created_at,
             client_ip, hazard_type, corroboration_area_key, authority_area_key,
             hazard_category, location_name, authority_area,
             detection_model, detection_model_version)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            image_path, source, damage_class, confidence, severity,
            lat, lon, description,
            int(ai_accepted) if ai_accepted is not None else None,
            status,
            datetime.now(timezone.utc).isoformat(),
            client_ip, hazard_type, corroboration_area_key, authority_area_key,
            hazard_category or "road_damage", location_name, authority_area,
            detection_model, detection_model_version,
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
# Clusters (repurposed: geographical organization for admin review)
# ---------------------------------------------------------------------------

def get_active_cluster(fine_key, hazard_category):
    conn = get_conn()
    row = conn.execute(
        """
        SELECT * FROM corroboration_clusters
        WHERE corroboration_area_key = ? AND hazard_category = ?
          AND lifecycle IN ('pending_approval','approved','missing_authority_email')
        ORDER BY id DESC LIMIT 1
        """,
        (fine_key, hazard_category),
    ).fetchone()
    conn.close()
    return row


def create_cluster(fine_key, hazard_category, authority_area_key=None,
                   representative_report_id=None):
    now = datetime.now(timezone.utc).isoformat()
    conn = get_conn()
    try:
        cur = conn.execute(
            """
            INSERT INTO corroboration_clusters
                (corroboration_area_key, hazard_category, authority_area_key,
                 representative_report_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (fine_key, hazard_category, authority_area_key,
             representative_report_id, now, now),
        )
        conn.commit()
        cluster_id = cur.lastrowid
    finally:
        conn.close()
    return get_cluster(cluster_id)


def get_cluster(cluster_id):
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM corroboration_clusters WHERE id = ?", (cluster_id,)
    ).fetchone()
    conn.close()
    return row


def set_report_cluster(report_id, cluster_id):
    conn = get_conn()
    conn.execute(
        "UPDATE detections SET cluster_id = ? WHERE id = ?",
        (cluster_id, report_id),
    )
    conn.commit()
    conn.close()


_CLUSTER_UPDATE_FIELDS = (
    "lifecycle", "letter_path", "sent_at", "last_send_error",
    "representative_report_id", "authority_area_key",
)


def update_cluster(cluster_id, **changes):
    conn = get_conn()
    fields = {k: v for k, v in changes.items() if k in _CLUSTER_UPDATE_FIELDS}
    fields["updated_at"] = datetime.now(timezone.utc).isoformat()
    if not fields:
        conn.close()
        return
    sets = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(
        f"UPDATE corroboration_clusters SET {sets} WHERE id = ?",
        (*fields.values(), cluster_id),
    )
    conn.commit()
    conn.close()


def list_clusters_for_admin():
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM corroboration_clusters ORDER BY updated_at DESC, id DESC"
    ).fetchall()
    conn.close()
    return rows


# ---------------------------------------------------------------------------
# Drafts
# ---------------------------------------------------------------------------

def list_expired_drafts(cutoff_iso):
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT id, image_path, letter_path FROM detections
        WHERE status = 'draft' AND created_at < ?
        """,
        (cutoff_iso,),
    ).fetchall()
    conn.close()
    return rows


def delete_draft(report_id):
    conn = get_conn()
    conn.execute(
        "DELETE FROM detections WHERE id = ? AND status = 'draft'", (report_id,)
    )
    conn.commit()
    conn.close()