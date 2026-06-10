from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from lagrange_bot.config import BotConfig
from lagrange_bot.models import MatchResult
from lagrange_bot.vision import (
    ScreenReader,
    read_command_value,
    read_match_timer_seconds,
    skill_slot_looks_ready,
    skill_slot_visual_diagnostics,
)


SAMPLE = {
    "matcher": {
        "playable_card_min_brightness": 90,
        "playable_card_min_bright_ratio": 0.35,
    },
    "screen": {"reference_size": [40, 40]},
    "phase": {"mode": "time", "read_screen_timer": False},
    "cost": {"mode": "debug_fixed", "debug_fixed_value": 100},
    "slots": {"hand": [{"name": "h1", "rect": [0, 0, 20, 20], "click": [10, 10]}]},
    "cards": [
        {
            "id": "card_a",
            "name": "Card A",
            "cost": 10,
            "row": "front",
            "template": "missing.png",
        }
    ],
}


class VisionTests(unittest.TestCase):
    def make_config(self) -> BotConfig:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp) / "configs"
            config_dir.mkdir()
            path = config_dir / "sample_config.json"
            path.write_text(json.dumps(SAMPLE), encoding="utf-8")
            return BotConfig.load(path)

    def test_dark_card_slot_is_not_visible_even_if_title_would_match(self):
        config = self.make_config()
        reader = ScreenReader(config)
        reader._match_card_title = lambda _image, _rect: MatchResult("card_a", 0.99, (0, 0, 10, 10))  # type: ignore[method-assign]

        cards = reader._read_cards(Image.new("RGB", (40, 40), (12, 12, 12)))

        self.assertEqual(cards, [])
        self.assertEqual(reader._slot_card_cache["h1"]["card_id"], "card_a")
        self.assertEqual(reader.last_hand_card_diagnostics["slots"]["h1"]["hidden_reason"], "not_playable")

    def test_cached_hand_card_becomes_visible_when_slot_turns_playable(self):
        config = self.make_config()
        config.screen["hand_card_read_interval_seconds"] = 2.0
        reader = ScreenReader(config)
        calls = {"titles": 0}

        def match_title(_image, _rect):
            calls["titles"] += 1
            return MatchResult("card_a", 0.99, (0, 0, 10, 10))

        reader._match_card_title = match_title  # type: ignore[method-assign]
        image = Image.new("RGB", (40, 40), (255, 255, 255))

        cards = reader._read_cards(image, hand_slot_playable={"h1": False}, current_cost=100)
        self.assertEqual(cards, [])

        cards = reader._read_cards(image, hand_slot_playable={"h1": True}, current_cost=100)
        self.assertEqual([card.card.id for card in cards], ["card_a"])
        self.assertEqual(calls["titles"], 1)
        self.assertEqual(reader.last_hand_card_diagnostics["cache_hits"], 1)

    def test_disabled_full_match_fallback_does_not_try_card_templates(self):
        config = self.make_config()
        config.screen["card_full_match_fallback"] = False
        reader = ScreenReader(config)
        reader._match_card_title = lambda _image, _rect: None  # type: ignore[method-assign]
        reader._get_card_templates = lambda: self.fail("full-card templates should not be read")  # type: ignore[method-assign]

        cards = reader._read_cards(
            Image.new("RGB", (40, 40), (255, 255, 255)),
            hand_slot_playable={"h1": True},
            current_cost=100,
        )

        self.assertEqual(cards, [])
        self.assertEqual(reader.last_hand_card_diagnostics["full_match_reads"], 0)

    def test_card_title_templates_can_load_multiple_live_samples(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "configs"
            config_dir.mkdir()
            live_dir = root / "templates" / "card_titles_live"
            live_dir.mkdir(parents=True)
            (live_dir / "card_a_01.png").write_bytes(b"sample-a")
            (live_dir / "card_a_02.png").write_bytes(b"sample-b")
            path = config_dir / "sample_config.json"
            data = json.loads(json.dumps(SAMPLE))
            data["matcher"]["card_title_live_templates_dir"] = "templates/card_titles_live"
            data["matcher"]["card_title_templates_dir"] = "templates/card_titles"
            path.write_text(json.dumps(data), encoding="utf-8")
            config = BotConfig.load(path)
            reader = ScreenReader(config)

            templates = reader._get_card_title_templates()

        self.assertEqual(len(templates["card_a"]), 2)
        self.assertTrue(templates["card_a"][0].endswith("card_a_01.png"))

    def test_unaffordable_matched_card_is_not_visible(self):
        config = self.make_config()
        reader = ScreenReader(config)
        reader._match_card_title = lambda _image, _rect: MatchResult("card_a", 0.99, (0, 0, 10, 10))  # type: ignore[method-assign]

        cards = reader._read_cards(
            Image.new("RGB", (40, 40), (255, 255, 255)),
            hand_slot_playable={"h1": True},
            current_cost=5,
        )

        self.assertEqual(cards, [])

    def test_cost_rects_ignore_hand_layout_offset(self):
        config = self.make_config()
        config.cost["number_rect"] = [10, 20, 30, 40]
        config.cost["rect"] = [50, 60, 70, 80]
        reader = ScreenReader(config)

        rects = reader._cost_rects_for_offset(55)

        self.assertEqual(
            rects,
            [
                ("number", (10, 20, 30, 40)),
                ("area", (50, 60, 70, 80)),
            ],
        )

    def test_timer_rects_ignore_hand_layout_offset(self):
        config = self.make_config()
        config.phase["timer_rects"] = [[10, 20, 30, 40]]
        reader = ScreenReader(config)

        rects = reader._timer_rects_for_offset(55)

        self.assertEqual(rects[0], (10, 20, 30, 40))
        self.assertNotIn((10, 75, 30, 40), rects)

    def test_match_timer_reads_live_timer_crop(self):
        root = Path(__file__).parent.parent
        raw_path = root / "logs" / "gui_sessions" / "20260609_000621_390997" / "0001_raw_星际猎人.png"
        if not raw_path.exists():
            self.skipTest("live timer sample is not available")
        image = Image.open(raw_path).convert("RGB")

        seconds = read_match_timer_seconds(
            image,
            (912, 20, 93, 30),
            root / "templates" / "timer_digits_clean",
        )

        self.assertEqual(seconds, 4.0)

    def test_command_value_reads_number_crop(self):
        root = Path(__file__).parent.parent
        crop_path = root / "training_samples" / "hand_live_20260608_195920_468682" / "command" / "00001_command_number.png"
        if not crop_path.exists():
            self.skipTest("live command-value sample is not available")
        image = Image.open(crop_path).convert("RGB")

        value = read_command_value(
            image,
            (0, 0, image.width, image.height),
            root / "templates" / "command_digits",
        )

        self.assertEqual(value, 24)

    def test_bright_skill_slot_looks_ready(self):
        config = self.make_config()
        image = Image.new("RGB", (20, 20), (70, 120, 180))

        diagnostics = skill_slot_visual_diagnostics(image, (0, 0, 20, 20))

        self.assertTrue(skill_slot_looks_ready(config, diagnostics))
        self.assertLessEqual(diagnostics["dark_ratio"], config.skill_ready_max_dark_ratio())

    def test_dark_cooldown_skill_slot_is_not_ready(self):
        config = self.make_config()
        image = Image.new("RGB", (20, 20), (80, 110, 150))
        for x in range(4, 16):
            for y in range(4, 16):
                image.putpixel((x, y), (5, 5, 5))

        diagnostics = skill_slot_visual_diagnostics(image, (0, 0, 20, 20))

        self.assertFalse(skill_slot_looks_ready(config, diagnostics))
        self.assertGreater(diagnostics["dark_ratio"], config.skill_ready_max_dark_ratio())

    def test_cooldown_only_skill_ignores_visual_gate_after_timer_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "configs"
            config_dir.mkdir()
            path = config_dir / "sample_config.json"
            data = json.loads(json.dumps(SAMPLE))
            data["skills"] = [
                {
                    "id": "skill_a",
                    "name": "Skill A",
                    "active": True,
                    "cooldown_seconds": 15,
                    "ready_mode": "cooldown_only",
                    "template": "missing_skill.png",
                    "rect": [0, 0, 20, 20],
                    "click": [10, 10],
                    "conditions": {},
                }
            ]
            path.write_text(json.dumps(data), encoding="utf-8")
            config = BotConfig.load(path)

        reader = ScreenReader(config)
        dark_image = Image.new("RGB", (40, 40), (5, 5, 5))

        state = reader._read_skills(dark_image, now_seconds=0)[0]
        self.assertTrue(state.ready)
        self.assertFalse(state.diagnostics["visual_ready"])

        reader.last_skill_cast["skill_a"] = 0.0
        self.assertFalse(reader._read_skills(dark_image, now_seconds=14.9)[0].ready)
        self.assertTrue(reader._read_skills(dark_image, now_seconds=15.0)[0].ready)

    def test_mark_action_executed_uses_game_seconds_for_skill_cooldown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "configs"
            config_dir.mkdir()
            path = config_dir / "sample_config.json"
            data = json.loads(json.dumps(SAMPLE))
            data["skills"] = [
                {
                    "id": "cover_tank",
                    "name": "Cover Tank",
                    "active": True,
                    "cooldown_seconds": 30,
                    "ready_mode": "cooldown_only",
                    "template": "missing_skill.png",
                    "rect": [0, 0, 20, 20],
                    "click": [10, 10],
                    "conditions": {},
                }
            ]
            path.write_text(json.dumps(data), encoding="utf-8")
            config = BotConfig.load(path)

        reader = ScreenReader(config)
        image = Image.new("RGB", (40, 40), (5, 5, 5))

        reader.mark_action_executed("cast_skill", "cover_tank", now_seconds=130.0)

        self.assertFalse(reader._read_skills(image, now_seconds=159.9)[0].ready)
        ready = reader._read_skills(image, now_seconds=160.0)[0]
        self.assertTrue(ready.ready)
        self.assertEqual(ready.seconds_since_cast, 30.0)

    def test_historical_battlefield_row_rect_uses_only_battle_frames_after_130_seconds(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "configs"
            samples_dir = root / "training_samples" / "battle_live_sample"
            config_dir.mkdir()
            samples_dir.mkdir(parents=True)
            manifest = samples_dir / "manifest.jsonl"
            records = [
                {
                    "screen_timer_seconds": 129,
                    "state": {
                        "phase": "battle",
                        "battlefield_targets": [
                            {"target_id": "cas066_battle_label", "rect": [300, 90, 174, 42]}
                        ],
                    },
                },
                {
                    "state": {
                        "phase": "battle",
                        "battlefield_targets": [
                            {"target_id": "cas066_battle_label", "rect": [300, 120, 174, 42]}
                        ],
                    },
                },
                {
                    "screen_timer_seconds": 130,
                    "state": {
                        "phase": "battle",
                        "battlefield_targets": [
                            {"target_id": "cas066_battle_label", "rect": [994, 302, 174, 42]}
                        ],
                    },
                },
                {
                    "state": {
                        "time": 150,
                        "phase": "battle",
                        "battlefield_targets": [
                            {"target_id": "cas066_battle_label", "rect": [994, 623, 174, 42]}
                        ],
                    },
                },
                {
                    "state": {
                        "time": 160,
                        "phase": "placement",
                        "battlefield_targets": [
                            {"target_id": "cas066_battle_label", "rect": [994, 800, 174, 42]}
                        ],
                    },
                },
            ]
            manifest.write_text("\n".join(json.dumps(item) for item in records), encoding="utf-8")

            data = json.loads(json.dumps(SAMPLE))
            data["battlefield"] = {
                "targets": [
                    {
                        "id": "cas066_battle_label",
                        "template": "missing.png",
                        "search_mode": "historical_row",
                        "historical_sources": ["training_samples"],
                        "historical_min_time_seconds": 130,
                        "historical_padding_y": 10,
                        "row_scan_x": 160,
                        "row_scan_width": 1600,
                        "search_rect": [160, 220, 1600, 560],
                    }
                ]
            }
            path = config_dir / "sample_config.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            config = BotConfig.load(path)

            reader = ScreenReader(config)
            rect, diagnostics = reader._battlefield_search_rect(config.data["battlefield"]["targets"][0])

            self.assertEqual(rect, (160, 292, 1600, 383))
            self.assertEqual(diagnostics["source"], "historical_samples")
            self.assertEqual(diagnostics["historical_min_time_seconds"], 130.0)
            self.assertEqual(diagnostics["frames_used"], 2)
            self.assertEqual(diagnostics["frames_without_time"], 1)
            self.assertEqual(diagnostics["sample_rect_count"], 2)
            self.assertEqual(diagnostics["sample_y_range"], [302, 623])


if __name__ == "__main__":
    unittest.main()
