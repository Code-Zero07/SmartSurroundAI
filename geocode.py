"""geocode.py
------------
Server-side reverse geocoding with a configurable endpoint (default: public
Nominatim; set GEOCODE_URL to a self-hosted Nominatim). The endpoint is a
SERVER-ONLY config — it is never exposed to the frontend.

Failure handling: a geocoder outage may affect human-readable labels only.
Routing never depends on this module — the coarse grid key is pure coordinate
math (server-derived). Submission is never blocked on geocoder availability.
"""

import json
import os
from urllib.parse import urlencode
from urllib.request import Request, urlopen

GEOCODE_URL = os.environ.get(
    "GEOCODE_URL", "https://nominatim.openstreetmap.org/reverse")
GEOCODE_TIMEOUT = float(os.environ.get("GEOCODE_TIMEOUT", "5"))
GEOCODE_USER_AGENT = os.environ.get(
    "GEOCODE_USER_AGENT", "SmartSurround/0.1 (admin@smartsurround.local)")


def _extract_fields(data):
    """location_name: the most specific readable tag. authority_area: a
    human area label built from suburb/neighbourhood + city/state."""
    addr = data.get("address") or {}
    location_name = (
        addr.get("road")
        or addr.get("pedestrian")
        or addr.get("neighbourhood")
        or addr.get("suburb")
        or addr.get("village")
        or addr.get("town")
        or addr.get("city")
    )
    area = (addr.get("suburb") or addr.get("neighbourhood")
            or addr.get("city_district"))
    city = (addr.get("city") or addr.get("town")
            or addr.get("village") or addr.get("state"))
    if area and city:
        authority_area = f"{area} / {city}"
    elif city:
        authority_area = city
    else:
        authority_area = area
    return location_name, authority_area


def reverse_geocode(lat, lon):
    """Always returns a dict: {"location_name": ..., "authority_area": ...}.
    Network/parse failures yield None labels (never raises into the caller)."""
    try:
        params = urlencode({
            "lat": f"{lat:.6f}", "lon": f"{lon:.6f}",
            "format": "jsonv2", "accept-language": "en",
        })
        url = f"{GEOCODE_URL}?{params}"
        req = Request(url, headers={"User-Agent": GEOCODE_USER_AGENT})
        with urlopen(req, timeout=GEOCODE_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        location_name, authority_area = _extract_fields(data)
        return {"location_name": location_name, "authority_area": authority_area}
    except Exception:
        return {"location_name": None, "authority_area": None}