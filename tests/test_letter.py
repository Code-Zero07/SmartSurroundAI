"""
tests/test_letter.py
--------------------
Letter PDF is generic over hazard category/type and falls back to legacy fields.

Run:  python -m unittest discover -s tests -p "test_letter.py" -v
"""

import os
import tempfile
import unittest
from unittest import mock

import letter_generator
from letter_generator import humanize_label


class LetterGeneratorTest(unittest.TestCase):

    def test_humanize_label(self):
        self.assertEqual(humanize_label("road_damage"), "Road Damage")
        self.assertEqual(humanize_label("waterlogged_road"), "Waterlogged Road")
        self.assertEqual(humanize_label(""), "Unspecified")
        self.assertEqual(humanize_label(None), "Unspecified")

    def _detection(self):
        return {
            "id": 7,
            "image_path": None,
            "damage_class": "pothole",
            "hazard_category": "road_damage",
            "hazard_type": "pothole",
            "confidence": 0.96,
            "severity": "Critical",
            "lat": 22.5710,
            "lon": 88.3639,
            "location_name": "Near College More, Sector V, Kolkata",
            "source": "citizen",
            "description": None,
            "created_at": "2026-09-18T10:00:00+00:00",
        }

    @mock.patch("letter_generator.LETTERS_DIR", new_callable=lambda: tempfile.mkdtemp(prefix="ss_letter_"))
    def test_generates_pdf_with_generic_text(self, letters_dir):
        path = letter_generator.generate_letter(self._detection())
        self.assertTrue(os.path.isfile(path))
        self.assertTrue(os.path.getsize(path) > 0)
        with open(path, "rb") as fh:
            raw = fh.read()
        self.assertIn(b"Road Damage Requiring Attention", raw)
        self.assertIn(b"Pothole", raw)
        self.assertIn(b"22.571", raw)
        self.assertNotIn(b"Report of Damaged Road Condition", raw)

    @mock.patch("letter_generator.LETTERS_DIR", new_callable=lambda: tempfile.mkdtemp(prefix="ss_letter_"))
    def test_waterlogging_fields_render_generically(self, letters_dir):
        d = self._detection()
        d["hazard_category"] = "waterlogging"
        d["hazard_type"] = "waterlogged_road"
        d["confidence"] = 0.93
        path = letter_generator.generate_letter(d)
        with open(path, "rb") as fh:
            raw = fh.read()
        self.assertIn(b"Waterlogging Requiring Attention", raw)
        self.assertIn(b"Waterlogged Road", raw)


if __name__ == "__main__":
    unittest.main()