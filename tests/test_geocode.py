"""
tests/test_geocode.py
---------------------
Reverse geocoding, network-free (urllib mocked).

Run:  python -m unittest discover -s tests -p "test_geocode.py" -v
"""

import io
import json
import unittest
from unittest import mock

import geocode


class _FakeResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._payload


def _json_body(payload: dict) -> _FakeResponse:
    return _FakeResponse(json.dumps(payload).encode("utf-8"))


class GeocodeTest(unittest.TestCase):

    @mock.patch.object(geocode, "urlopen")
    def test_reverse_geocode_returns_labels(self, urlopen):
        urlopen.return_value = _json_body({
            "address": {
                "road": "College More",
                "suburb": "Sector V",
                "city": "Kolkata",
            },
        })
        got = geocode.reverse_geocode(22.5710, 88.3639)
        self.assertEqual(got["location_name"], "College More")
        self.assertEqual(got["authority_area"], "Sector V / Kolkata")

    @mock.patch.object(geocode, "urlopen")
    def test_reverse_geocode_uses_configured_endpoint(self, urlopen):
        urlopen.return_value = _json_body({"address": {"city": "Kolkata"}})
        geocode.GEOCODE_URL = "https://geo.internal/reverse"
        try:
            geocode.reverse_geocode(1.0, 2.0)
        finally:
            geocode.GEOCODE_URL = (
                "https://nominatim.openstreetmap.org/reverse")
        url = urlopen.call_args[0][0].full_url
        self.assertTrue(url.startswith("https://geo.internal/reverse"), url)
        self.assertIn("lat=1.000000", url)
        self.assertIn("lon=2.000000", url)
        ua = urlopen.call_args[0][0].headers.get("User-agent")
        self.assertIn("SmartSurround", ua)

    @mock.patch.object(geocode, "urlopen")
    def test_outage_yields_none_labels(self, urlopen):
        urlopen.side_effect = OSError("network unreachable")
        got = geocode.reverse_geocode(22.5710, 88.3639)
        self.assertEqual(got, {"location_name": None, "authority_area": None})

    @mock.patch.object(geocode, "urlopen")
    def test_missing_address_yields_none_labels(self, urlopen):
        urlopen.return_value = _json_body({})
        got = geocode.reverse_geocode(22.5710, 88.3639)
        self.assertEqual(got["location_name"], None)
        self.assertEqual(got["authority_area"], None)

    @mock.patch.object(geocode, "urlopen")
    def test_area_label_falls_back_to_city_only(self, urlopen):
        urlopen.return_value = _json_body({"address": {"city": "Kolkata"}})
        got = geocode.reverse_geocode(22.5710, 88.3639)
        self.assertEqual(got["authority_area"], "Kolkata")


if __name__ == "__main__":
    unittest.main()