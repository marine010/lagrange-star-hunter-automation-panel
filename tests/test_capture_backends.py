from __future__ import annotations

import unittest
from unittest.mock import patch

from lagrange_bot.capture_backends import (
    _is_wgc_border_toggle_unsupported,
    _optional_bool,
    _wgc_crop_box,
    _wgc_draw_border_attempts,
)
from lagrange_bot.windowing import WindowInfo


class CaptureBackendTests(unittest.TestCase):
    def test_optional_bool_parses_auto_values_as_none(self):
        for value in (None, "", "auto", "default", "none", "null"):
            with self.subTest(value=value):
                self.assertIsNone(_optional_bool(value))

    def test_optional_bool_parses_boolean_strings(self):
        self.assertTrue(_optional_bool("true"))
        self.assertTrue(_optional_bool("1"))
        self.assertFalse(_optional_bool("false"))
        self.assertFalse(_optional_bool("0"))

    def test_wgc_draw_border_attempts_default_does_not_toggle(self):
        self.assertEqual(_wgc_draw_border_attempts(None), (None,))

    def test_wgc_draw_border_attempts_fallback_to_default(self):
        self.assertEqual(_wgc_draw_border_attempts(False), (False, None))
        self.assertEqual(_wgc_draw_border_attempts(True), (True, None))

    def test_wgc_border_toggle_unsupported_detects_wrapped_error(self):
        inner = RuntimeError(
            "Graphics capture error: Toggling the capture border is not supported "
            "by the Graphics Capture API on this platform."
        )
        outer = RuntimeError("Capture session threw an exception")
        outer.__cause__ = inner

        self.assertTrue(_is_wgc_border_toggle_unsupported(outer))

    def test_wgc_border_toggle_unsupported_ignores_other_errors(self):
        exc = RuntimeError("Graphics capture error: access denied")

        self.assertFalse(_is_wgc_border_toggle_unsupported(exc))

    def test_wgc_crop_box_maps_extended_frame_to_client_area(self):
        window = WindowInfo(
            hwnd=123,
            title="Star Hunter",
            rect=(330, 335, 2276, 1486),
            client_rect=(343, 393, 2263, 1473),
        )
        monitor = {"left": 343, "top": 393, "width": 1920, "height": 1080}

        with patch(
            "lagrange_bot.capture_backends._extended_frame_bounds",
            return_value=(341, 335, 2265, 1475),
        ):
            crop_box = _wgc_crop_box(window, monitor, (1924, 1140))

        self.assertEqual(crop_box, (2, 58, 1922, 1138))


if __name__ == "__main__":
    unittest.main()
