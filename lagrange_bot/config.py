from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import CardDefinition, Rect, SkillDefinition, SlotConfig


class ConfigError(ValueError):
    pass


def _as_rect(value: Any, key: str) -> Rect:
    if not isinstance(value, list) or len(value) != 4:
        raise ConfigError(f"{key} must be [left, top, width, height]")
    return tuple(int(v) for v in value)  # type: ignore[return-value]


def _as_point(value: Any, key: str) -> tuple[int, int]:
    if not isinstance(value, list) or len(value) != 2:
        raise ConfigError(f"{key} must be [x, y]")
    return int(value[0]), int(value[1])


def _resolve_path(root: Path, path_value: str | None) -> str | None:
    if not path_value:
        return None
    path = Path(path_value)
    if path.is_absolute():
        return str(path)
    return str((root / path).resolve())


class BotConfig:
    def __init__(self, data: dict[str, Any], path: Path):
        self.data = data
        self.path = path
        self.root = path.parent.parent.resolve()
        self.profile_name = str(data.get("profile_name", "lagrange_bot"))
        self.dry_run_default = bool(data.get("dry_run_default", True))
        self.loop_interval_seconds = float(data.get("loop_interval_seconds", 0.35))
        self.global_click_delay_seconds = float(data.get("global_click_delay_seconds", 0.08))
        self.matcher = data.get("matcher", {})
        self.screen = data.get("screen", {})
        self.cost = data.get("cost", {})
        self.phase = data.get("phase", {})
        self.policy = data.get("policy", {})
        self.hand_slots = self._load_hand_slots()
        self.reserve_slot = self._load_reserve_slot()
        self.cards = self._load_cards()
        self.skills = self._load_skills()

    @classmethod
    def load(cls, path: str | Path) -> "BotConfig":
        config_path = Path(path).resolve()
        with config_path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        return cls(data, config_path)

    def card_threshold(self) -> float:
        return float(self.matcher.get("card_threshold", 0.82))

    def skill_threshold(self) -> float:
        return float(self.matcher.get("skill_threshold", 0.82))

    def skill_ready_max_dark_ratio(self) -> float:
        return float(self.matcher.get("skill_ready_max_dark_ratio", 0.55))

    def skill_ready_min_brightness(self) -> float:
        return float(self.matcher.get("skill_ready_min_brightness", 45))

    def digit_threshold(self) -> float:
        return float(self.matcher.get("digit_threshold", 0.78))

    def battlefield_threshold(self) -> float:
        return float(self.matcher.get("battlefield_threshold", 0.80))

    def capture_rect(self) -> Rect | None:
        value = self.screen.get("capture_rect")
        if value is None:
            return None
        return _as_rect(value, "screen.capture_rect")

    def _load_hand_slots(self) -> list[SlotConfig]:
        slots = self.data.get("slots", {}).get("hand", [])
        if not slots:
            raise ConfigError("slots.hand must contain at least one slot")
        return [
            SlotConfig(
                name=str(item["name"]),
                rect=_as_rect(item["rect"], f"slots.hand[{index}].rect"),
                click=_as_point(item["click"], f"slots.hand[{index}].click"),
            )
            for index, item in enumerate(slots)
        ]

    def _load_reserve_slot(self) -> SlotConfig | None:
        item = self.data.get("slots", {}).get("reserve")
        if not item:
            return None
        return SlotConfig(
            name=str(item["name"]),
            rect=_as_rect(item["rect"], "slots.reserve.rect"),
            click=_as_point(item["click"], "slots.reserve.click"),
        )

    def _load_cards(self) -> dict[str, CardDefinition]:
        cards: dict[str, CardDefinition] = {}
        for item in self.data.get("cards", []):
            card_id = str(item["id"])
            cards[card_id] = CardDefinition(
                id=card_id,
                name=str(item.get("name", card_id)),
                cost=int(item.get("cost", 0)),
                row=str(item.get("row", "unknown")),  # type: ignore[arg-type]
                template=str(_resolve_path(self.root, item.get("template"))),
                priority=int(item.get("priority", 0)),
                max_plays=int(item.get("max_plays", 99)),
                conditions=dict(item.get("conditions", {})),
            )
        if not cards:
            raise ConfigError("cards must contain at least one card")
        return cards

    def _load_skills(self) -> list[SkillDefinition]:
        skills: list[SkillDefinition] = []
        for item in self.data.get("skills", []):
            rect = item.get("rect")
            click = item.get("click")
            select_target_group = item.get("select_target_group")
            select_click = item.get("select_click")
            target_click = item.get("target_click")
            skills.append(
                SkillDefinition(
                    id=str(item["id"]),
                    name=str(item.get("name", item["id"])),
                    active=bool(item.get("active", True)),
                    cooldown_seconds=float(item.get("cooldown_seconds", 0)),
                    template=_resolve_path(self.root, item.get("template")),
                    rect=_as_rect(rect, f"skills.{item['id']}.rect") if rect else None,
                    click=_as_point(click, f"skills.{item['id']}.click") if click else None,
                    ready_mode=str(item.get("ready_mode", "visual_and_cooldown")),
                    select_target_group=str(select_target_group) if select_target_group else None,
                    select_click=(
                        _as_point(select_click, f"skills.{item['id']}.select_click")
                        if select_click
                        else None
                    ),
                    target_click=(
                        _as_point(target_click, f"skills.{item['id']}.target_click")
                        if target_click
                        else None
                    ),
                    target_mode=str(item.get("target_mode", "none")),
                    priority=int(item.get("priority", 0)),
                    conditions=dict(item.get("conditions", {})),
                )
            )
        return skills
