from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal


Rect = tuple[int, int, int, int]
Point = tuple[int, int]
Row = Literal["front", "middle", "back", "unknown"]


class Phase(str, Enum):
    PLACEMENT = "placement"
    EXTRA_PLACE = "extra_place"
    BATTLE = "battle"
    UNKNOWN = "unknown"


class ActionType(str, Enum):
    WAIT = "wait"
    PLAY_CARD = "play_card"
    CAST_SKILL = "cast_skill"


@dataclass(frozen=True)
class SlotConfig:
    name: str
    rect: Rect
    click: Point


@dataclass(frozen=True)
class MatchResult:
    item_id: str
    confidence: float
    rect: Rect


@dataclass(frozen=True)
class CardDefinition:
    id: str
    name: str
    cost: int
    row: Row
    template: str
    priority: int = 0
    max_plays: int = 99
    conditions: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SkillDefinition:
    id: str
    name: str
    active: bool
    cooldown_seconds: float
    template: str | None
    rect: Rect | None
    click: Point | None
    ready_mode: str = "visual_and_cooldown"
    select_target_group: str | None = None
    select_click: Point | None = None
    target_click: Point | None = None
    target_mode: str = "none"
    priority: int = 0
    conditions: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class VisibleCard:
    card: CardDefinition
    slot: SlotConfig
    confidence: float


@dataclass(frozen=True)
class SkillState:
    skill: SkillDefinition
    ready: bool
    confidence: float = 0.0
    seconds_since_cast: float | None = None
    diagnostics: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BattlefieldTarget:
    target_id: str
    name: str
    confidence: float
    rect: Rect
    click: Point


@dataclass
class GameState:
    now_seconds: float
    phase: Phase
    cost: int
    visible_cards: list[VisibleCard]
    reserve_card_id: str | None = None
    played_counts: dict[str, int] = field(default_factory=dict)
    skills: list[SkillState] = field(default_factory=list)
    battlefield_targets: list[BattlefieldTarget] = field(default_factory=list)
    hand_slot_playable: dict[str, bool] = field(default_factory=dict)
    layout_offset_x: int = 0
    layout_offset_y: int = 0
    capture_origin: Point = (0, 0)
    capture_scale: tuple[float, float] = (1.0, 1.0)


@dataclass(frozen=True)
class Action:
    type: ActionType
    reason: str
    pre_clicks: tuple[Point, ...] = ()
    click: Point | None = None
    card_id: str | None = None
    skill_id: str | None = None
    target_click: Point | None = None
    wait_seconds: float = 0.0

    @staticmethod
    def wait(reason: str, wait_seconds: float = 0.25) -> "Action":
        return Action(ActionType.WAIT, reason=reason, wait_seconds=wait_seconds)
