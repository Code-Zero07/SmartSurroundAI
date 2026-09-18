"""corroboration.py
-------------------
Geographical clustering for ADMIN ORGANIZATION.

This is NOT corroboration and NOT a send authorization:
  - clustering groups nearby reports for the admin to review;
  - admin approval (cluster_service.py) is the ONLY send trigger;
  - a single valid report can form its own cluster.

Grid model (kept from the old Track B design):
  - FINE key   (~111 m cells): cluster grouping unit.
  - COARSE key (~1.1 km cells): belongs to ONE authority territory; used for
    authority routing (coarse key + hazard_category).

Cluster generations: at most one ACTIVE cluster per (fine key, hazard_category)
is enforced by the partial unique index in db.py (see CLUSTER_ACTIVE_LIFECYCLES).
`sent`/`settled` clusters are terminal: a later same-area report starts a NEW
generation instead of reopening the old one.
"""

import os
import sqlite3
from datetime import datetime, timezone, timedelta

import db

# --- config surfaces (env, all optional, with safe defaults) -----------------
FINE_DECIMALS = int(os.environ.get("CORROBORATION_FINE_DECIMALS", "3"))      # ~111 m
COARSE_DECIMALS = int(os.environ.get("CORROBORATION_COARSE_DECIMALS", "2"))  # ~1.1 km
DRAFT_TTL_HOURS = int(os.environ.get("DRAFT_TTL_HOURS", "24"))

ACTIVE_LIFECYCLES = db.CLUSTER_ACTIVE_LIFECYCLES
ELIGIBLE_REPORT_STATUSES = ("pending", "approved")


def _round_cell(value, decimals):
    return round(value, decimals)


def fine_area_key(lat, lon):
    if lat is None or lon is None:
        return None
    return f"{_round_cell(lat, FINE_DECIMALS)}:{_round_cell(lon, FINE_DECIMALS)}"


def coarse_area_key(lat, lon):
    if lat is None or lon is None:
        return None
    return f"{_round_cell(lat, COARSE_DECIMALS)}:{_round_cell(lon, COARSE_DECIMALS)}"


# ---------------------------------------------------------------------------
# Cluster membership
# ---------------------------------------------------------------------------

def assign_report_to_cluster(report_id, fine_key, hazard_category,
                             authority_area_key=None):
    """Join a report to the ACTIVE (fine key, hazard_category) cluster, or
    create a new generation. One-active-cluster is enforced by the partial
    unique index; a creation race is resolved by re-joining the winner."""
    cluster = db.get_active_cluster(fine_key, hazard_category)
    if cluster is None:
        try:
            cluster = db.create_cluster(
                fine_key, hazard_category, authority_area_key=authority_area_key)
        except sqlite3.IntegrityError:
            cluster = db.get_active_cluster(fine_key, hazard_category)
            if cluster is None:
                raise
    db.set_report_cluster(report_id, cluster["id"])
    recompute_representative(cluster["id"])
    return cluster["id"]


# ---------------------------------------------------------------------------
# Representative evidence (highest-confidence VALID report)
# ---------------------------------------------------------------------------

def recompute_representative(cluster_id):
    """Highest-confidence report with status IN (pending, approved) becomes the
    representative; refresh the cluster's authority_area_key from its coords."""
    cluster = db.get_cluster(cluster_id)
    if cluster is None:
        return None
    conn = db.get_conn()
    row = conn.execute(
        """
        SELECT * FROM detections
        WHERE cluster_id = ?
          AND status IN ('pending', 'approved')
        ORDER BY (confidence IS NULL) ASC, confidence DESC, id ASC
        LIMIT 1
        """,
        (cluster_id,),
    ).fetchone()
    conn.close()
    if row is None:
        db.update_cluster(cluster_id, representative_report_id=None,
                          authority_area_key=None)
        return None
    db.update_cluster(cluster_id,
                      representative_report_id=row["id"],
                      authority_area_key=row["authority_area_key"])
    return row["id"]


def recompute_for_report(report_id):
    """After a report joins or its review status changes, refresh the
    representative of its cluster."""
    report = db.get_detection(report_id)
    if report is None or report["cluster_id"] is None:
        return None
    return recompute_representative(report["cluster_id"])


def reports_in_cluster(cluster_id):
    conn = db.get_conn()
    rows = conn.execute(
        """
        SELECT * FROM detections
        WHERE cluster_id = ?
        ORDER BY (confidence IS NULL) ASC, confidence DESC, id ASC
        """,
        (cluster_id,),
    ).fetchall()
    conn.close()
    return rows


# ---------------------------------------------------------------------------
# Admin payload + draft hygiene
# ---------------------------------------------------------------------------

def _report_dict(row):
    if row is None:
        return None
    image_url = None
    if row["image_path"]:
        image_url = f"/uploads/{os.path.basename(row['image_path'])}"
    return {
        "id": row["id"],
        "status": row["status"],
        "image_url": image_url,
        "hazard_category": row["hazard_category"],
        "hazard_type": row["hazard_type"],
        "confidence": row["confidence"],
        "location_name": row["location_name"],
        "lat": row["lat"],
        "lon": row["lon"],
        "source": row["source"],
        "created_at": row["created_at"],
    }


def clusters_for_admin():
    out = []
    for c in db.list_clusters_for_admin():
        reports = reports_in_cluster(c["id"])
        rep = None
        if c["representative_report_id"]:
            rep = db.get_detection(c["representative_report_id"])
        out.append({
            "id": c["id"],
            "corroboration_area_key": c["corroboration_area_key"],
            "hazard_category": c["hazard_category"],
            "lifecycle": c["lifecycle"],
            "letter_path": c["letter_path"],
            "sent_at": c["sent_at"],
            "last_send_error": c["last_send_error"],
            "representative_report_id": c["representative_report_id"],
            "authority_area_key": c["authority_area_key"],
            "created_at": c["created_at"],
            "updated_at": c["updated_at"],
            "report_count": sum(1 for r in reports
                                if r["status"] in ELIGIBLE_REPORT_STATUSES),
            "representative": _report_dict(rep),
            "submissions": [_report_dict(r) for r in reports],
        })
    return out


def sweep_expired_drafts():
    cutoff = (datetime.now(timezone.utc)
              - timedelta(hours=DRAFT_TTL_HOURS)).isoformat()
    drafts = db.list_expired_drafts(cutoff)
    removed = 0
    base = os.path.dirname(__file__)
    for d in drafts:
        for sub in ("uploads", "letters"):
            raw = d["image_path"] if sub == "uploads" else d["letter_path"]
            if not raw:
                continue
            try:
                p = os.path.join(base, sub, os.path.basename(raw))
                if os.path.isfile(p):
                    os.remove(p)
            except OSError:
                pass
        db.delete_draft(d["id"])
        removed += 1
    return removed