from __future__ import annotations

import unittest
from unittest.mock import patch

from lagrange_bot.capture_backends import _wgc_crop_box
from lagrange_bot.windowing import WindowInfo


class CaptureBackendTests(unittest.TestCase):
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
