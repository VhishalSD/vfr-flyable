#!/usr/bin/env python3

import sys
import unittest
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from airport_magnetic import (  # noqa: E402
    MODEL_NAME,
    MagneticModelError,
    calculate_signed_declination,
    magnetic_heading,
    variation_fields,
)


class AirportMagneticTests(unittest.TestCase):
    def test_east_west_sign_convention(self):
        east = variation_fields(2.84, date(2026, 8, 30))
        west = variation_fields(-4.26, date(2026, 8, 30))
        self.assertEqual((east["magneticVariationDeg"], east["magneticVariationDirection"]), (2.8, "E"))
        self.assertEqual((west["magneticVariationDeg"], west["magneticVariationDirection"]), (4.3, "W"))

    def test_true_to_magnetic_aviation_convention(self):
        self.assertEqual(magnetic_heading(109.0, 2.8), 106)
        self.assertEqual(magnetic_heading(109.0, -2.8), 112)

    def test_wraparound_and_rounding(self):
        self.assertEqual(magnetic_heading(1.0, 2.0), 359)
        self.assertEqual(magnetic_heading(359.0, -2.0), 1)
        self.assertEqual(magnetic_heading(109.8, 2.621433), 107)
        self.assertEqual(magnetic_heading(359.49, 0), 359)
        self.assertEqual(magnetic_heading(359.50, 0), 0)

    def test_missing_heading_or_declination(self):
        self.assertIsNone(magnetic_heading(None, 2.0))
        self.assertIsNone(magnetic_heading(100, None))

    def test_missing_or_invalid_coordinates(self):
        with self.assertRaises(MagneticModelError):
            calculate_signed_declination(None, 4.46, date(2026, 8, 30))
        with self.assertRaises(MagneticModelError):
            calculate_signed_declination(91, 4.46, date(2026, 8, 30))

    def test_wmm2025_is_deterministic_for_fixed_date(self):
        first = calculate_signed_declination(51.190667, 4.463153, date(2026, 8, 30), 11.8872)
        second = calculate_signed_declination(51.190667, 4.463153, date(2026, 8, 30), 11.8872)
        self.assertEqual(first, second)
        self.assertAlmostEqual(first, 2.621433, places=6)
        self.assertEqual(variation_fields(first, date(2026, 8, 30))["magneticVariationSource"], MODEL_NAME)


if __name__ == "__main__":
    unittest.main()
