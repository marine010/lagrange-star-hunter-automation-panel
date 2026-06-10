from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lagrange_bot.config import BotConfig
from lagrange_bot.decision import DeckPolicy
from lagrange_bot.gui import LagrangeTestGui, _live_skill_target_confirmation
from lagrange_bot.models import Action, ActionType, BattlefieldTarget, GameState, Phase, SkillState, SlotConfig, VisibleCard


SAMPLE = {
    "slots": {
        "hand": [{"name": "h1", "rect": [0, 0, 10, 10], "click": [5, 5]}]
    },
    "cards": [
        {
            "id": "tank",
            "name": "Tank",
            "cost": 40,
            "row": "front",
            "template": "templates/cards/tank.png",
            "priority": 100,
            "max_plays": 2,
            "conditions": {},
        },
        {
            "id": "dps",
            "name": "DPS",
            "cost": 30,
            "row": "middle",
            "template": "templates/cards/dps.png",
            "priority": 50,
            "conditions": {"requires_played_any": ["tank"]},
        },
    ],
    "policy": {"opening_sequence": ["tank", "dps"]},
}


class DecisionTests(unittest.TestCase):
    def make_config(self) -> BotConfig:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sample_config.json"
            import json

            path.write_text(json.dumps(SAMPLE), encoding="utf-8")
            return BotConfig.load(path)

    def test_opening_sequence_prefers_tank(self):
        config = self.make_config()
        slot = SlotConfig("h1", (0, 0, 10, 10), (5, 5))
        state = GameState(
            now_seconds=1,
            phase=Phase.PLACEMENT,
            cost=100,
            visible_cards=[VisibleCard(config.cards["tank"], slot, 0.99)],
            played_counts={},
        )
        action = DeckPolicy(config).decide(state)
        self.assertEqual(action.card_id, "tank")

    def test_dps_waits_for_tank_condition(self):
        config = self.make_config()
        slot = SlotConfig("h1", (0, 0, 10, 10), (5, 5))
        state = GameState(
            now_seconds=1,
            phase=Phase.PLACEMENT,
            cost=100,
            visible_cards=[VisibleCard(config.cards["dps"], slot, 0.99)],
            played_counts={},
        )
        action = DeckPolicy(config).decide(state)
        self.assertEqual(action.type.value, "wait")

    def test_deployment_waits_until_start_time(self):
        config = BotConfig.load(Path(__file__).parent.parent / "configs" / "star_hunter_1920.json")
        slot = SlotConfig("h1", (0, 0, 10, 10), (5, 5))
        state = GameState(
            now_seconds=70,
            phase=Phase.PLACEMENT,
            cost=250,
            visible_cards=[VisibleCard(config.cards["cas066"], slot, 0.99)],
            played_counts={},
        )
        action = DeckPolicy(config).decide(state)
        self.assertEqual(action.type.value, "wait")

    def test_deployment_prefers_066_after_start_time(self):
        config = BotConfig.load(Path(__file__).parent.parent / "configs" / "star_hunter_1920.json")
        slots = [
            SlotConfig("h1", (0, 0, 10, 10), (5, 5)),
            SlotConfig("h2", (0, 0, 10, 10), (15, 5)),
        ]
        state = GameState(
            now_seconds=76,
            phase=Phase.PLACEMENT,
            cost=50,
            visible_cards=[
                VisibleCard(config.cards["stingray"], slots[0], 0.99),
                VisibleCard(config.cards["cas066"], slots[1], 0.99),
            ],
            played_counts={},
            hand_slot_playable={slot.name: True for slot in config.hand_slots},
        )
        action = DeckPolicy(config).decide(state)
        self.assertEqual(action.card_id, "cas066")
        self.assertEqual(action.click, (15, 5))

    def test_deployment_waits_for_all_hand_slots_playable(self):
        config = BotConfig.load(Path(__file__).parent.parent / "configs" / "star_hunter_1920.json")
        slot = config.hand_slots[0]
        state = GameState(
            now_seconds=76,
            phase=Phase.PLACEMENT,
            cost=50,
            visible_cards=[VisibleCard(config.cards["cas066"], slot, 0.99)],
            played_counts={},
            hand_slot_playable={
                config.hand_slots[0].name: True,
                config.hand_slots[1].name: True,
                config.hand_slots[2].name: False,
                config.hand_slots[3].name: True,
            },
        )
        action = DeckPolicy(config).decide(state)
        self.assertEqual(action.type.value, "wait")
        self.assertIn("waiting for all hand cards playable", action.reason)

    def test_extra_place_does_not_wait_for_all_hand_slots_playable(self):
        config = BotConfig.load(Path(__file__).parent.parent / "configs" / "star_hunter_1920.json")
        slot = config.hand_slots[0]
        state = GameState(
            now_seconds=122,
            phase=Phase.EXTRA_PLACE,
            cost=20,
            visible_cards=[VisibleCard(config.cards["stingray"], slot, 0.99)],
            played_counts={},
            hand_slot_playable={
                config.hand_slots[0].name: True,
                config.hand_slots[1].name: False,
                config.hand_slots[2].name: False,
                config.hand_slots[3].name: False,
            },
        )
        action = DeckPolicy(config).decide(state)
        self.assertEqual(action.card_id, "stingray")

    def test_deployment_fallback_prefers_rainsea_before_stingray(self):
        config = BotConfig.load(Path(__file__).parent.parent / "configs" / "star_hunter_1920.json")
        slots = [
            SlotConfig("h1", (0, 0, 10, 10), (5, 5)),
            SlotConfig("h2", (0, 0, 10, 10), (15, 5)),
        ]
        state = GameState(
            now_seconds=122,
            phase=Phase.EXTRA_PLACE,
            cost=20,
            visible_cards=[
                VisibleCard(config.cards["stingray"], slots[0], 0.99),
                VisibleCard(config.cards["rainsea_assault"], slots[1], 0.99),
            ],
            played_counts={},
            hand_slot_playable={slot.name: True for slot in config.hand_slots},
        )

        action = DeckPolicy(config).decide(state)

        self.assertEqual(action.card_id, "rainsea_assault")
        self.assertEqual(action.click, (15, 5))

    def test_battle_cover_tank_selects_tank_066_group(self):
        config = BotConfig.load(Path(__file__).parent.parent / "configs" / "star_hunter_1920.json")
        cover_tank = next(skill for skill in config.skills if skill.id == "cover_tank")
        damage_boost = next(skill for skill in config.skills if skill.id == "damage_boost")
        state = GameState(
            now_seconds=140,
            phase=Phase.BATTLE,
            cost=0,
            visible_cards=[],
            skills=[
                SkillState(skill=damage_boost, ready=True),
                SkillState(skill=cover_tank, ready=True),
            ],
        )
        action = DeckPolicy(config).decide(state)
        self.assertEqual(action.skill_id, "cover_tank")
        expected = tuple(config.policy["battle"]["target_groups"]["cas066_tank_group"])
        self.assertEqual(action.click, cover_tank.click)
        self.assertEqual(action.target_click, expected)
        self.assertEqual(action.pre_clicks, ())

    def test_battle_skills_wait_before_130_seconds(self):
        config = BotConfig.load(Path(__file__).parent.parent / "configs" / "star_hunter_1920.json")
        cover_tank = next(skill for skill in config.skills if skill.id == "cover_tank")
        state = GameState(
            now_seconds=129,
            phase=Phase.BATTLE,
            cost=0,
            visible_cards=[],
            skills=[SkillState(skill=cover_tank, ready=True)],
        )
        action = DeckPolicy(config).decide(state)
        self.assertIsNone(action.skill_id)
        self.assertEqual(action.type.value, "wait")

    def test_battle_cover_tank_uses_dynamic_topmost_066_group(self):
        config = BotConfig.load(Path(__file__).parent.parent / "configs" / "star_hunter_1920.json")
        cover_tank = next(skill for skill in config.skills if skill.id == "cover_tank")
        state = GameState(
            now_seconds=140,
            phase=Phase.BATTLE,
            cost=0,
            visible_cards=[],
            skills=[SkillState(skill=cover_tank, ready=True)],
            battlefield_targets=[
                BattlefieldTarget("cas066_battle_label", "CAS066", 0.91, (900, 620, 120, 45), (960, 645)),
                BattlefieldTarget("cas066_battle_label", "CAS066", 0.94, (1030, 350, 120, 45), (1090, 375)),
                BattlefieldTarget("cas066_battle_label", "CAS066", 0.93, (980, 500, 120, 45), (1040, 525)),
            ],
        )
        action = DeckPolicy(config).decide(state)
        self.assertEqual(action.skill_id, "cover_tank")
        self.assertEqual(action.click, cover_tank.click)
        self.assertEqual(action.target_click, (1090, 375))
        self.assertEqual(action.pre_clicks, ())

    def test_battle_damage_boost_uses_dynamic_bottommost_066_group(self):
        config = BotConfig.load(Path(__file__).parent.parent / "configs" / "star_hunter_1920.json")
        damage_boost = next(skill for skill in config.skills if skill.id == "damage_boost")
        state = GameState(
            now_seconds=140,
            phase=Phase.BATTLE,
            cost=0,
            visible_cards=[],
            skills=[SkillState(skill=damage_boost, ready=True)],
            battlefield_targets=[
                BattlefieldTarget("cas066_battle_label", "CAS066", 0.91, (900, 620, 120, 45), (960, 645)),
                BattlefieldTarget("cas066_battle_label", "CAS066", 0.94, (1030, 350, 120, 45), (1090, 375)),
                BattlefieldTarget("cas066_battle_label", "CAS066", 0.93, (980, 500, 120, 45), (1040, 525)),
            ],
        )
        action = DeckPolicy(config).decide(state)
        self.assertEqual(action.skill_id, "damage_boost")
        self.assertEqual(action.click, damage_boost.click)
        self.assertEqual(action.target_click, (960, 645))
        self.assertEqual(action.pre_clicks, ())

    def test_battle_single_detected_066_uses_distinct_fallback_groups(self):
        config = BotConfig.load(Path(__file__).parent.parent / "configs" / "star_hunter_1920.json")
        cover_tank = next(skill for skill in config.skills if skill.id == "cover_tank")
        damage_boost = next(skill for skill in config.skills if skill.id == "damage_boost")
        target = BattlefieldTarget("cas066_battle_label", "CAS066", 0.91, (900, 620, 120, 45), (960, 645))

        cover_state = GameState(
            now_seconds=140,
            phase=Phase.BATTLE,
            cost=0,
            visible_cards=[],
            skills=[SkillState(skill=cover_tank, ready=True)],
            battlefield_targets=[target],
        )
        damage_state = GameState(
            now_seconds=140,
            phase=Phase.BATTLE,
            cost=0,
            visible_cards=[],
            skills=[SkillState(skill=damage_boost, ready=True)],
            battlefield_targets=[target],
        )

        cover_action = DeckPolicy(config).decide(cover_state)
        damage_action = DeckPolicy(config).decide(damage_state)

        self.assertEqual(cover_action.target_click, tuple(cover_tank.select_click))
        self.assertEqual(damage_action.target_click, tuple(damage_boost.select_click))
        self.assertNotEqual(cover_action.target_click, damage_action.target_click)

    def test_live_skill_target_confirmation_requires_two_066_groups(self):
        config = BotConfig.load(Path(__file__).parent.parent / "configs" / "star_hunter_1920.json")
        state = GameState(
            now_seconds=140,
            phase=Phase.BATTLE,
            cost=0,
            visible_cards=[],
            battlefield_targets=[
                BattlefieldTarget("cas066_battle_label", "CAS066", 0.91, (900, 620, 120, 45), (960, 645)),
            ],
        )

        confirmation = _live_skill_target_confirmation(config, "damage_boost", state)

        self.assertTrue(confirmation["required"])
        self.assertFalse(confirmation["confirmed"])
        self.assertEqual(confirmation["reason"], "insufficient_candidates")
        self.assertEqual(confirmation["candidate_count"], 1)
        self.assertIsNone(confirmation["target_click"])

    def test_live_skill_target_confirmation_reselects_current_066_group(self):
        config = BotConfig.load(Path(__file__).parent.parent / "configs" / "star_hunter_1920.json")
        state = GameState(
            now_seconds=140,
            phase=Phase.BATTLE,
            cost=0,
            visible_cards=[],
            battlefield_targets=[
                BattlefieldTarget("cas066_battle_label", "CAS066", 0.91, (900, 620, 120, 45), (960, 645)),
                BattlefieldTarget("cas066_battle_label", "CAS066", 0.94, (1030, 350, 120, 45), (1090, 375)),
                BattlefieldTarget("cas066_battle_label", "CAS066", 0.93, (980, 500, 120, 45), (1040, 525)),
            ],
        )

        tank_confirmation = _live_skill_target_confirmation(config, "cover_tank", state)
        damage_confirmation = _live_skill_target_confirmation(config, "damage_boost", state)

        self.assertTrue(tank_confirmation["confirmed"])
        self.assertEqual(tank_confirmation["target_click"], (1090, 375))
        self.assertTrue(damage_confirmation["confirmed"])
        self.assertEqual(damage_confirmation["target_click"], (960, 645))

    def test_live_skill_target_refresh_falls_back_to_existing_target_click(self):
        config = BotConfig.load(Path(__file__).parent.parent / "configs" / "star_hunter_1920.json")
        gui = LagrangeTestGui.__new__(LagrangeTestGui)
        gui._continuous_window = None
        action = Action(
            type=ActionType.CAST_SKILL,
            skill_id="cover_tank",
            click=(1099, 976),
            target_click=(1019, 366),
            reason="cast skill cover_tank",
        )
        state = GameState(
            now_seconds=140,
            phase=Phase.BATTLE,
            cost=0,
            visible_cards=[],
            battlefield_targets=[],
        )

        refresh = gui._refresh_live_skill_target_before_click(action, state, config, reader=None)

        self.assertTrue(refresh["ok"])
        self.assertTrue(refresh["fallback"])
        self.assertEqual(refresh["reason"], "target_refresh_fallback")
        self.assertIs(refresh["action"], action)
        self.assertEqual(refresh["target_confirmation"]["candidate_count"], 0)
        self.assertEqual(refresh["target_confirmation"]["fallback_reason"], "target_refresh_missing_window")
        self.assertEqual(refresh["target_confirmation"]["fallback_target_click"], (1019, 366))
        self.assertEqual(refresh["target_confirmation"]["execution"], "target_confirmation_unverified")

    def test_live_skill_target_refresh_still_runs_when_current_frame_is_confirmed(self):
        config = BotConfig.load(Path(__file__).parent.parent / "configs" / "star_hunter_1920.json")
        gui = LagrangeTestGui.__new__(LagrangeTestGui)
        gui._continuous_window = None
        action = Action(
            type=ActionType.CAST_SKILL,
            skill_id="cover_tank",
            click=(1099, 976),
            target_click=(1019, 366),
            reason="cast skill cover_tank",
        )
        state = GameState(
            now_seconds=140,
            phase=Phase.BATTLE,
            cost=0,
            visible_cards=[],
            battlefield_targets=[
                BattlefieldTarget("cas066_battle_label", "CAS066", 0.91, (900, 620, 120, 45), (960, 645)),
                BattlefieldTarget("cas066_battle_label", "CAS066", 0.94, (1030, 350, 120, 45), (1090, 375)),
            ],
        )

        refresh = gui._refresh_live_skill_target_before_click(action, state, config, reader=None)

        self.assertTrue(refresh["ok"])
        self.assertTrue(refresh["fallback"])
        self.assertEqual(refresh["reason"], "target_refresh_fallback")
        self.assertIs(refresh["action"], action)
        self.assertEqual(refresh["target_confirmation"]["fallback_reason"], "target_refresh_missing_window")
        self.assertEqual(refresh["target_confirmation"]["fallback_target_click"], (1019, 366))
        self.assertEqual(refresh["target_confirmation"]["execution"], "target_confirmation_unverified")

    def test_live_skill_target_refresh_blocks_without_existing_target_click(self):
        config = BotConfig.load(Path(__file__).parent.parent / "configs" / "star_hunter_1920.json")
        gui = LagrangeTestGui.__new__(LagrangeTestGui)
        gui._continuous_window = None
        action = Action(
            type=ActionType.CAST_SKILL,
            skill_id="cover_tank",
            click=(1099, 976),
            target_click=None,
            reason="cast skill cover_tank",
        )
        state = GameState(
            now_seconds=140,
            phase=Phase.BATTLE,
            cost=0,
            visible_cards=[],
            battlefield_targets=[],
        )

        refresh = gui._refresh_live_skill_target_before_click(action, state, config, reader=None)

        self.assertFalse(refresh["ok"])
        self.assertEqual(refresh["reason"], "target_refresh_missing_window")
        self.assertEqual(refresh["target_confirmation"]["candidate_count"], 0)
        self.assertIsNone(refresh["target_confirmation"]["fallback_target_click"])


if __name__ == "__main__":
    unittest.main()
