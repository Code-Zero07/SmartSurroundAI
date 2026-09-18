"""cluster_service.py
---------------------
Admin-authorized cluster send orchestration.

Guards enforced here (never in the route):
  - `approve` only from pending_approval; sets `approved` INDEPENDENT of send
    success; then attempts the send.
  - `send` only from approved/missing_authority_email AND sent_at IS NULL.
  - `sent` is terminal: sent_at written only on success; failure records
    last_send_error and leaves sent_at null (retryable via /send).
  - `settled` is terminal and not sendable.
  - Authority lookup: coarse grid key + hazard_category (never location_name).
  - One email per send; PDF letter only (representative photo embedded);
    only the highest-confidence eligible representative is used.
"""

import logging
from datetime import datetime, timezone

import db
import authority_routing
import email_driver
import letter_generator
import corroboration

logger = logging.getLogger("smart_surround.cluster")

SENDABLE_LIFECYCLES = ("approved", "missing_authority_email")


def _compose_subject(hazard_category, authority_area_key):
    label = letter_generator.humanize_label(hazard_category)
    return f"SmartSurround {label} Authority Notice - area {authority_area_key}"


def _compose_body(rep, fine_key, report_count):
    lat = f"{rep['lat']:.6f}" if rep["lat"] is not None else "N/A"
    lon = f"{rep['lon']:.6f}" if rep["lon"] is not None else "N/A"
    label = letter_generator.humanize_label(rep["hazard_category"])
    htype = letter_generator.humanize_label(
        rep["hazard_type"] or rep["damage_class"])
    confidence = (f"{round(rep['confidence'] * 100, 1)}%"
                  if rep["confidence"] is not None else "N/A")
    location = rep["location_name"] or rep["authority_area_key"] or "N/A"
    return (
        f"SmartSurround authority notice - {label} requiring attention.\n\n"
        f"Hazard:                       {label}\n"
        f"Type:                         {htype}\n"
        f"Confidence:                   {confidence}\n"
        f"Authority area (coarse key):  {rep['authority_area_key']}\n"
        f"Fine cluster area:            {fine_key}\n"
        f"Citizen reports in cluster:   {report_count}\n"
        f"Location:                     {location}\n"
        f"Coordinates:                  lat {lat}, lon {lon}\n\n"
        "Please find the generated authority letter attached."
    )


def _representative(cluster):
    if not cluster["representative_report_id"]:
        return None
    return db.get_detection(cluster["representative_report_id"])


def _send(cluster_id):
    """Shared resumable finalizer (used by approve and by /send retry)."""
    cluster = db.get_cluster(cluster_id)
    if cluster is None:
        return {"ok": False, "message": "Cluster not found."}
    if cluster["sent_at"] is not None or cluster["lifecycle"] == "sent":
        return {"ok": False, "message": "Cluster already sent."}
    if cluster["lifecycle"] not in SENDABLE_LIFECYCLES:
        return {"ok": False,
                "message": f"Cannot send cluster in state {cluster['lifecycle']}."}
    rep = _representative(cluster)
    if rep is None or not rep["authority_area_key"]:
        return {"ok": False, "message": "Cluster has no valid representative."}
    owner = authority_routing.resolve_owner(rep["authority_area_key"],
                                            rep["hazard_category"])
    if owner is None:
        db.update_cluster(cluster_id, lifecycle="missing_authority_email",
                          last_send_error=None)
        return {"ok": False, "message": "No verified authority email for this area.",
                "state": "missing_authority_email"}
    count = sum(1 for r in corroboration.reports_in_cluster(cluster_id)
                if r["status"] in corroboration.ELIGIBLE_REPORT_STATUSES)
    letter_path = letter_generator.generate_letter(rep)
    result = email_driver.send_authority_email(
        owner["email"],
        _compose_subject(rep["hazard_category"], rep["authority_area_key"]),
        _compose_body(rep, cluster["corroboration_area_key"], count),
        letter_path,
    )
    if not result.get("ok"):
        reason = result.get("reason") or "unknown_email_error"
        db.update_cluster(cluster_id, last_send_error=reason)
        logger.warning("cluster %s send failed (soft): %s", cluster_id, reason)
        return {"ok": False, "message": "Email send failed; see cluster error.",
                "reason": reason}
    db.update_cluster(cluster_id, lifecycle="sent",
                      sent_at=datetime.now(timezone.utc).isoformat(),
                      letter_path=letter_path, last_send_error=None)
    return {"ok": True, "message": "Email sent and cluster marked sent."}


def approve_and_maybe_send(cluster_id):
    """Approve sets approved independently of send success, then attempts send."""
    cluster = db.get_cluster(cluster_id)
    if cluster is None:
        return {"ok": False, "message": "Cluster not found."}
    if cluster["lifecycle"] == "sent" or cluster["lifecycle"] == "settled":
        return {"ok": False,
                "message": f"Cluster is already {cluster['lifecycle']}."}
    if cluster["lifecycle"] != "pending_approval":
        return {"ok": False,
                "message": f"Cluster cannot be approved from state {cluster['lifecycle']}."}
    db.update_cluster(cluster_id, lifecycle="approved")
    return _send(cluster_id)


def send_cluster_email(cluster_id):
    return _send(cluster_id)


def settle_cluster(cluster_id):
    cluster = db.get_cluster(cluster_id)
    if cluster is None:
        return {"ok": False, "message": "Cluster not found."}
    if cluster["lifecycle"] in ("sent", "settled"):
        return {"ok": False,
                "message": f"Cluster is already {cluster['lifecycle']}."}
    db.update_cluster(cluster_id, lifecycle="settled")
    return {"ok": True, "message": "Cluster settled."}


def provide_authority_email(cluster_id, email):
    email = (email or "").strip()
    if authority_routing._looks_bogus(email):
        return {"ok": False, "message": "That email address looks invalid."}
    cluster = db.get_cluster(cluster_id)
    if cluster is None:
        return {"ok": False, "message": "Cluster not found."}
    if cluster["lifecycle"] not in ("approved", "missing_authority_email"):
        return {"ok": False,
                "message": f"Cannot attach email in state {cluster['lifecycle']}."}
    rep = _representative(cluster)
    if rep is None or not rep["authority_area_key"]:
        return {"ok": False, "message": "Cluster has no valid representative."}
    # Admin-confirmed for routing; delivery/bounce state stays separately
    # trackable on the authorities row (pending/verified/bounced lifecycle).
    owner = db.get_or_create_authority(rep["authority_area_key"],
                                       rep["hazard_category"], email)
    db.mark_authority_verified(owner["id"])
    db.update_cluster(cluster_id, lifecycle="approved")
    return _send(cluster_id)