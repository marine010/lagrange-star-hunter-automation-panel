from __future__ import annotations

import argparse
import json
import queue
import subprocess
import sys
import threading
import time
import traceback
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from .windowing import WindowInfo, enable_dpi_awareness, focus_window, get_window, list_visible_windows, set_window_topmost

enable_dpi_awareness()

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageDraw, ImageTk

from .capture_backends import capture_window_client, stop_wgc_sessions
from .config import BotConfig
from .decision import DeckPolicy
from .local_calibration import save_local_calibration
from .models import Action, ActionType, GameState, Phase
from .vision import (
    ScreenReader,
    calibrate_layout_offset,
    card_title_rect_for_slot,
    crop_image,
    normalize_capture_image,
    slot_looks_playable,
)


DEFAULT_CONFIG = Path("configs/star_hunter_1920.json")
CONTINUOUS_CAPTURE_INTERVAL_MS = 250
CONTINUOUS_TARGET_FRAME_MS = 1000
CONTINUOUS_MIN_DELAY_MS = 50
CONTINUOUS_MAX_DELAY_MS = 1000


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _safe_file_stem(text: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in text.strip())
    safe = safe.strip("_")
    return safe[:64] or "item"


class GuiSessionLogger:
    def __init__(self, config_path: Path, base_dir: Path | None = None):
        self.started_at = datetime.now().astimezone()
        self.session_id = self.started_at.strftime("%Y%m%d_%H%M%S_%f")
        self.dir = (base_dir or Path("logs") / "gui_sessions") / self.session_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.jsonl_path = self.dir / "events.jsonl"
        self.text_path = self.dir / "session.txt"
        self._lock = threading.Lock()
        self._image_counter = 0
        self.event(
            "session_start",
            {
                "session_id": self.session_id,
                "config_path": str(config_path.resolve()),
                "cwd": str(Path.cwd()),
            },
        )

    def event(self, name: str, payload: dict[str, Any] | None = None) -> None:
        record = {"time": _now_iso(), "event": name, "payload": payload or {}}
        text = json.dumps(record, ensure_ascii=False, default=str)
        pretty_payload = json.dumps(payload or {}, ensure_ascii=False, indent=2, default=str)
        with self._lock:
            with self.jsonl_path.open("a", encoding="utf-8") as handle:
                handle.write(text + "\n")
            with self.text_path.open("a", encoding="utf-8") as handle:
                handle.write(f"[{record['time']}] {name}\n")
                handle.write(pretty_payload + "\n\n")

    def save_image(self, label: str, image: Image.Image) -> Path:
        with self._lock:
            self._image_counter += 1
            path = self.dir / f"{self._image_counter:04d}_{_safe_file_stem(label)}.png"
        image.save(path)
        return path


def _state_to_dict(state: GameState) -> dict[str, Any]:
    return {
        "time": round(state.now_seconds, 2),
        "phase": state.phase.value,
        "cost": state.cost,
        "capture_origin": state.capture_origin,
        "capture_scale": state.capture_scale,
        "layout_offset_x": state.layout_offset_x,
        "layout_offset_y": state.layout_offset_y,
        "hand_slot_playable": state.hand_slot_playable,
        "visible_cards": [
            {
                "slot": item.slot.name,
                "card_id": item.card.id,
                "name": item.card.name,
                "cost": item.card.cost,
                "confidence": round(item.confidence, 4),
                "click": item.slot.click,
            }
            for item in state.visible_cards
        ],
        "reserve_card_id": state.reserve_card_id,
        "skills": [
            {
                "skill_id": item.skill.id,
                "name": item.skill.name,
                "ready": item.ready,
                "confidence": round(item.confidence, 4),
                "seconds_since_cast": (
                    round(item.seconds_since_cast, 2)
                    if item.seconds_since_cast is not None
                    else None
                ),
                "diagnostics": item.diagnostics,
            }
            for item in state.skills
        ],
        "battlefield_targets": [
            {
                "target_id": item.target_id,
                "name": item.name,
                "confidence": round(item.confidence, 4),
                "rect": item.rect,
                "click": item.click,
            }
            for item in state.battlefield_targets
        ],
    }


def _action_to_dict(action: Action) -> dict[str, Any]:
    return {
        "action": action.type.value,
        "reason": action.reason,
        "pre_clicks": list(action.pre_clicks),
        "click": action.click,
        "target_click": action.target_click,
        "card_id": action.card_id,
        "skill_id": action.skill_id,
        "wait_seconds": action.wait_seconds,
    }


def _slot_sort_key(slot_name: str) -> tuple[str, int, str]:
    prefix, separator, suffix = slot_name.rpartition("_")
    if separator:
        try:
            return prefix, int(suffix), slot_name
        except ValueError:
            pass
    return slot_name, 0, slot_name


def _format_hand_summary(payload: dict[str, Any]) -> str:
    diagnostics = (
        payload.get("vision_diagnostics", {})
        .get("hand_card_diagnostics", {})
        .get("slots", {})
    )
    if diagnostics:
        parts: list[str] = []
        for slot_name in sorted(diagnostics, key=_slot_sort_key):
            slot = diagnostics.get(slot_name, {})
            short_slot = slot_name.replace("hand_", "h")
            card_id = slot.get("card_id") or "?"
            if slot.get("visible"):
                status = "可出"
            elif slot.get("hidden_reason") == "not_playable" or not slot.get("playable"):
                status = "灰牌"
            elif slot.get("hidden_reason") == "unaffordable":
                status = "费用不足"
            else:
                status = "未显示"
            parts.append(f"{short_slot}: {card_id}[{status}]")
        return " | ".join(parts)

    visible_cards = payload.get("state", {}).get("visible_cards", [])
    if visible_cards:
        return " | ".join(f"{item.get('slot')}: {item.get('card_id')}" for item in visible_cards)
    return "无"


def _format_skill_summary(payload: dict[str, Any]) -> str:
    skills = payload.get("state", {}).get("skills", [])
    if not skills:
        return "无"
    parts = []
    for item in skills:
        skill_id = item.get("skill_id")
        ready = "就绪" if item.get("ready") else "冷却"
        confidence = item.get("confidence")
        diagnostics = item.get("diagnostics") if isinstance(item.get("diagnostics"), dict) else {}
        best_id = diagnostics.get("best_template_id")
        best_score = diagnostics.get("best_template_score")
        dark_ratio = diagnostics.get("dark_ratio")
        if isinstance(confidence, (int, float)) and confidence > 0:
            suffix = f"{confidence:.2f}"
            if isinstance(best_id, str) and isinstance(best_score, (int, float)):
                suffix += f"/best={best_id}:{best_score:.2f}"
            if isinstance(dark_ratio, (int, float)):
                suffix += f"/dark={dark_ratio:.2f}"
            parts.append(f"{skill_id}[{ready}/{suffix}]")
        else:
            parts.append(f"{skill_id}[{ready}]")
    return " | ".join(parts)


def _format_battlefield_summary(payload: dict[str, Any]) -> str:
    targets = payload.get("state", {}).get("battlefield_targets", [])
    if not targets:
        return "无"
    parts = []
    for index, item in enumerate(targets, start=1):
        click = item.get("click")
        confidence = item.get("confidence")
        confidence_text = f"{confidence:.2f}" if isinstance(confidence, (int, float)) else "?"
        parts.append(f"{index}:{item.get('target_id')}@{click}/{confidence_text}")
    return " | ".join(parts)


def _format_confidence(value: Any) -> str:
    if isinstance(value, (int, float)) and float(value) > 0:
        return f"{float(value):.2f}"
    return "--"


def _offset_point(
    point: tuple[int, int] | None,
    origin: tuple[int, int],
    scale: tuple[float, float],
) -> tuple[int, int] | None:
    if point is None:
        return None
    return int(round(point[0] * scale[0])) + origin[0], int(round(point[1] * scale[1])) + origin[1]


def _display_action_to_dict(
    action: Action,
    origin: tuple[int, int],
    scale: tuple[float, float],
) -> dict[str, Any]:
    return {
        **_action_to_dict(action),
        "screen_pre_clicks": [_offset_point(point, origin, scale) for point in action.pre_clicks],
        "screen_click": _offset_point(action.click, origin, scale),
        "screen_target_click": _offset_point(action.target_click, origin, scale),
    }


def _live_skill_target_confirmation(
    config: BotConfig,
    skill_id: str | None,
    state: GameState,
) -> dict[str, Any]:
    skill = next((item for item in config.skills if item.id == skill_id), None)
    if skill is None or not skill.select_target_group:
        return {
            "required": False,
            "confirmed": True,
            "skill_id": skill_id,
            "reason": "no_dynamic_target_group",
        }

    target_group = skill.select_target_group
    selector = config.policy.get("battle", {}).get("dynamic_target_groups", {}).get(target_group)
    if not selector:
        return {
            "required": False,
            "confirmed": True,
            "skill_id": skill_id,
            "target_group": target_group,
            "reason": "no_dynamic_selector",
        }

    target_id = str(selector.get("target_id", "cas066_battle_label"))
    min_candidates = max(1, int(selector.get("min_candidates", 1)))
    candidates = [target for target in state.battlefield_targets if target.target_id == target_id]
    target_click = DeckPolicy(config)._select_skill_target(target_group, state)
    confirmed = target_click is not None and len(candidates) >= min_candidates
    return {
        "required": True,
        "confirmed": confirmed,
        "reason": "confirmed" if confirmed else "insufficient_candidates",
        "skill_id": skill_id,
        "target_group": target_group,
        "target_id": target_id,
        "sort": selector.get("sort", "topmost"),
        "index": selector.get("index", 0),
        "min_candidates": min_candidates,
        "candidate_count": len(candidates),
        "candidates": [
            {
                "target_id": target.target_id,
                "confidence": round(target.confidence, 4),
                "rect": target.rect,
                "click": target.click,
            }
            for target in candidates
        ],
        "target_click": target_click,
    }


def _rect_xyxy(rect: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    left, top, width, height = rect
    return left, top, left + width, top + height


def _offset_rect(rect: tuple[int, int, int, int], dx: int = 0, dy: int = 0) -> tuple[int, int, int, int]:
    return rect[0] + dx, rect[1] + dy, rect[2], rect[3]


def _hand_slots_for_state(config: BotConfig, state: GameState):
    if not state.layout_offset_x and not state.layout_offset_y:
        return config.hand_slots
    return [
        slot.__class__(
            name=slot.name,
            rect=_offset_rect(slot.rect, state.layout_offset_x, state.layout_offset_y),
            click=(slot.click[0] + state.layout_offset_x, slot.click[1] + state.layout_offset_y),
        )
        for slot in config.hand_slots
    ]


def _draw_point(draw: ImageDraw.ImageDraw, point: tuple[int, int], color: str, label: str) -> None:
    x, y = point
    radius = 9
    draw.ellipse((x - radius, y - radius, x + radius, y + radius), outline=color, width=3)
    draw.line((x - 14, y, x + 14, y), fill=color, width=2)
    draw.line((x, y - 14, x, y + 14), fill=color, width=2)
    draw.text((x + 12, y - 12), label, fill=color)


def annotate_image(
    image: Image.Image,
    config: BotConfig,
    state: GameState,
    action: Action,
    hand_card_diagnostics: dict[str, Any] | None = None,
) -> Image.Image:
    annotated = image.convert("RGB").copy()
    draw = ImageDraw.Draw(annotated)

    for slot in config.hand_slots:
        draw.rectangle(_rect_xyxy(slot.rect), outline="#3b82f6", width=2)
        draw.text((slot.rect[0] + 4, slot.rect[1] + 4), slot.name, fill="#3b82f6")

    if config.reserve_slot:
        slot = config.reserve_slot
        draw.rectangle(_rect_xyxy(slot.rect), outline="#8b5cf6", width=2)
        draw.text((slot.rect[0] + 4, slot.rect[1] + 4), slot.name, fill="#8b5cf6")

    if state.layout_offset_x or state.layout_offset_y:
        for slot in config.hand_slots:
            shifted = _offset_rect(slot.rect, state.layout_offset_x, state.layout_offset_y)
            draw.rectangle(_rect_xyxy(shifted), outline="#22c55e", width=2)
            draw.text(
                (shifted[0] + 4, shifted[1] + 4),
                f"{slot.name}+{state.layout_offset_x},{state.layout_offset_y}",
                fill="#22c55e",
            )

    for skill in config.skills:
        if skill.rect:
            draw.rectangle(_rect_xyxy(skill.rect), outline="#f97316", width=2)
            draw.text((skill.rect[0] + 4, skill.rect[1] + 4), skill.id, fill="#f97316")

    visible_slots = {visible.slot.name for visible in state.visible_cards}
    diagnostics_by_slot = (hand_card_diagnostics or {}).get("slots", {})
    for slot in [*_hand_slots_for_state(config, state)]:
        slot_diag = diagnostics_by_slot.get(slot.name, {})
        card_id = slot_diag.get("card_id")
        if not card_id:
            continue
        confidence = float(slot_diag.get("confidence") or 0.0)
        playable = bool(slot_diag.get("playable"))
        is_visible = slot.name in visible_slots or bool(slot_diag.get("visible"))
        color = "#22c55e" if is_visible else ("#facc15" if playable else "#94a3b8")
        width = 4 if is_visible else 3
        label = f"{card_id} {confidence:.2f}"
        if not is_visible:
            reason = slot_diag.get("hidden_reason")
            if reason:
                label = f"{label} {reason}"
        draw.rectangle(_rect_xyxy(slot.rect), outline=color, width=width)
        draw.text((slot.rect[0] + 4, slot.rect[1] + 22), label, fill=color)

    for target in state.battlefield_targets:
        label = f"{target.target_id} {target.confidence:.2f}"
        draw.rectangle(_rect_xyxy(target.rect), outline="#06b6d4", width=3)
        draw.text((target.rect[0] + 4, target.rect[1] - 14), label, fill="#06b6d4")
        _draw_point(draw, target.click, "#06b6d4", "target")

    for index, point in enumerate(action.pre_clicks, start=1):
        _draw_point(draw, point, "#eab308", f"pre{index}")
    if action.click:
        _draw_point(draw, action.click, "#ef4444", "click")
    if action.target_click:
        _draw_point(draw, action.target_click, "#ec4899", "final")

    return annotated


def validate_assets(config: BotConfig) -> dict[str, Any]:
    assets: list[dict[str, Any]] = []
    missing = 0

    for card in config.cards.values():
        path = Path(card.template)
        exists = path.exists()
        missing += 0 if exists else 1
        assets.append(
            {
                "kind": "card",
                "id": card.id,
                "name": card.name,
                "exists": exists,
                "path": str(path),
            }
        )

    for skill in config.skills:
        if not skill.template:
            continue
        path = Path(skill.template)
        exists = path.exists()
        missing += 0 if exists else 1
        assets.append(
            {
                "kind": "skill",
                "id": skill.id,
                "name": skill.name,
                "exists": exists,
                "path": str(path),
            }
        )

    for item in config.data.get("battlefield", {}).get("targets", []):
        path_text = item.get("template")
        path = Path(path_text)
        if not path.is_absolute():
            path = config.root / path
        exists = path.exists()
        missing += 0 if exists else 1
        assets.append(
            {
                "kind": "battlefield",
                "id": str(item["id"]),
                "name": str(item.get("name", item["id"])),
                "exists": exists,
                "path": str(path),
            }
        )

    return {"missing": missing, "assets": assets}


def _select_stable_calibration_sample(
    samples: list[dict[str, Any]],
    *,
    required_count: int,
    tolerance: int,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    valid = [sample for sample in samples if sample.get("ok")]
    required_count = max(1, int(required_count))
    tolerance = max(0, int(tolerance))
    if not valid:
        best_sample = max(
            samples,
            key=lambda item: float(item.get("result", {}).get("score", 0.0)),
            default=None,
        )
        return best_sample, {
            "ok": False,
            "reason": "no_sample_reached_min_score",
            "required_count": required_count,
            "tolerance": tolerance,
            "stable_count": 0,
        }

    best_group: list[dict[str, Any]] = []
    for anchor in valid:
        anchor_result = anchor.get("result", {})
        anchor_x = int(anchor_result.get("offset_x", 0))
        anchor_y = int(anchor_result.get("offset_y", 0))
        group = []
        for sample in valid:
            result = sample.get("result", {})
            offset_x = int(result.get("offset_x", 0))
            offset_y = int(result.get("offset_y", 0))
            if abs(offset_x - anchor_x) <= tolerance and abs(offset_y - anchor_y) <= tolerance:
                group.append(sample)
        group.sort(key=lambda item: float(item.get("result", {}).get("score", 0.0)), reverse=True)
        if (
            len(group) > len(best_group)
            or (
                len(group) == len(best_group)
                and group
                and sum(float(item.get("result", {}).get("score", 0.0)) for item in group)
                > sum(float(item.get("result", {}).get("score", 0.0)) for item in best_group)
            )
        ):
            best_group = group

    selected = best_group[0] if best_group else valid[0]
    selected_result = selected.get("result", {})
    stable = len(best_group) >= required_count
    return selected, {
        "ok": stable,
        "reason": "stable" if stable else "insufficient_stable_samples",
        "required_count": required_count,
        "tolerance": tolerance,
        "stable_count": len(best_group),
        "offset_x": int(selected_result.get("offset_x", 0)),
        "offset_y": int(selected_result.get("offset_y", 0)),
        "sample_indexes": [int(item.get("frame_index", 0)) for item in best_group],
        "scores": [float(item.get("result", {}).get("score", 0.0)) for item in best_group],
    }


def capture_window(window: WindowInfo, config: BotConfig) -> tuple[Image.Image, tuple[int, int], dict[str, int]]:
    return capture_window_client(window, config)


class LagrangeTestGui(tk.Tk):
    def __init__(self, config_path: Path | None = None):
        super().__init__()
        self.title("拉格朗日自动识别")
        self.geometry("640x680")
        self.minsize(600, 630)

        self.config_path_var = tk.StringVar(value=str((config_path or DEFAULT_CONFIG).resolve()))
        self.image_path_var = tk.StringVar(value="")
        self.time_var = tk.StringVar(value="76")
        self.phase_override_var = tk.StringVar(value="auto")
        self.cost_override_enabled = tk.BooleanVar(value=False)
        self.cost_override_var = tk.StringVar(value="120")
        self.status_var = tk.StringVar(value="Ready")
        self.window_var = tk.StringVar(value="")
        self._windows: list[WindowInfo] = []
        self.continuous_window_var = tk.BooleanVar(value=False)
        self.live_play_card_var = tk.BooleanVar(value=True)
        self.live_skill_var = tk.BooleanVar(value=True)
        self.hand_training_var = tk.BooleanVar(value=False)
        self.battle_training_var = tk.BooleanVar(value=False)
        self.deck_selected_count_var = tk.StringVar(value="")
        self.deck_card_vars: dict[str, tk.BooleanVar] = {}
        self.deck_card_buttons: list[tk.Checkbutton] = []
        self.deck_card_order: list[str] = []
        self.deck_selector_frame: ttk.Frame | None = None
        self._active_deck_card_ids: list[str] | None = None

        self._queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._photo: ImageTk.PhotoImage | None = None
        self._last_annotated: Image.Image | None = None
        self._worker_busy = False
        self._continuous_window: WindowInfo | None = None
        self._continuous_reader: ScreenReader | None = None
        self._continuous_count = 0
        self._continuous_after_id: str | None = None
        self._continuous_last_processing_ms: float | None = None
        self._continuous_last_delay_ms = 0
        self._last_continuous_capture_rect: tuple[int, int, int, int] | None = None
        self._last_continuous_rect_change_monotonic: float | None = None
        self._last_live_play_card_at = 0.0
        self._last_live_play_card_signature: tuple[str | None, tuple[int, int] | None] | None = None
        self._last_live_skill_at = 0.0
        self._last_live_skill_signature: tuple[str | None, tuple[int, int] | None, tuple[int, int] | None] | None = None
        self._pending_live_skill: dict[str, Any] | None = None
        self._opening_zoom_done = False
        self._opening_zoom_active = False
        self._topmost_hwnd: int | None = None
        self._hand_training_active = False
        self._hand_training_started_continuous = False
        self._hand_training_count = 0
        self._hand_training_dir: Path | None = None
        self._hand_training_manifest_path: Path | None = None
        self._hand_training_external = False
        self._hand_training_process: subprocess.Popen[Any] | None = None
        self._hand_training_stdout: Any | None = None
        self._hand_training_stderr: Any | None = None
        self._hand_training_lock = threading.Lock()
        self._battle_training_active = False
        self._battle_training_process: subprocess.Popen[Any] | None = None
        self._battle_training_stdout: Any | None = None
        self._battle_training_stderr: Any | None = None
        self._battle_training_dir: Path | None = None
        self._battle_training_manifest_path: Path | None = None
        self._battle_training_lock = threading.Lock()
        self.logger = GuiSessionLogger(Path(self.config_path_var.get()))
        self._build_ui()
        self.logger.event("ui_ready", {"log_dir": str(self.logger.dir.resolve())})
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._poll_queue)

    def _build_ui(self) -> None:
        self.configure(background="#0f172a")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)
        self._build_styles()

        controls = ttk.Frame(self, padding=(8, 6), style="Top.TFrame")
        controls.grid(row=0, column=0, sticky="ew")
        controls.columnconfigure(1, weight=1)

        ttk.Label(controls, text="拉格朗日自动识别", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        self.window_combo = ttk.Combobox(
            controls,
            textvariable=self.window_var,
            state="readonly",
            postcommand=self.refresh_windows_clicked,
        )
        self.window_combo.grid(row=0, column=1, sticky="ew", padx=(8, 6))
        self.continuous_button = ttk.Button(
            controls,
            text="开始识别",
            command=self.toggle_continuous_window_clicked,
            style="Accent.TButton",
        )
        self.continuous_button.grid(row=0, column=2)
        self.calibration_button = ttk.Button(
            controls,
            text="自动校准",
            command=self.auto_calibrate_clicked,
        )
        self.calibration_button.grid(row=0, column=3, padx=(6, 0))
        self.hand_training_button = ttk.Button(
            controls,
            text="开始手牌采集",
            command=self.toggle_hand_training_clicked,
        )
        self.battle_training_button = ttk.Button(
            controls,
            text="开始战斗采集",
            command=self.toggle_battle_training_clicked,
        )

        self.deck_selector_frame = ttk.Frame(self, padding=(8, 0, 8, 6), style="App.TFrame")
        self.deck_selector_frame.grid(row=1, column=0, sticky="ew")
        self._build_deck_selector()

        dashboard = ttk.Frame(self, padding=(8, 8), style="App.TFrame")
        dashboard.grid(row=2, column=0, sticky="nsew")
        dashboard.columnconfigure(0, weight=1)
        dashboard.columnconfigure(1, weight=1)
        dashboard.rowconfigure(1, weight=1)

        self.time_value_var = tk.StringVar(value="--")
        self.cost_value_var = tk.StringVar(value="--")
        self.calibration_value_var = tk.StringVar(value="未完成")
        self.phase_value_var = tk.StringVar(value="等待开始")
        self.last_action_var = tk.StringVar(value="自动放置手牌和技能释放已开启")
        self.hand_card_vars: list[dict[str, tk.StringVar]] = []
        self.skill_card_vars: dict[str, dict[str, tk.StringVar]] = {}
        self.hand_status_labels: list[ttk.Label] = []
        self.skill_status_labels: dict[str, ttk.Label] = {}

        metrics = ttk.Frame(dashboard, style="App.TFrame")
        metrics.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        metrics.columnconfigure(0, weight=1)
        metrics.columnconfigure(1, weight=1)
        metrics.columnconfigure(2, weight=1)
        self._build_metric_card(metrics, 0, "时间", self.time_value_var, "s")
        self._build_metric_card(metrics, 1, "费用", self.cost_value_var, "")
        self._build_metric_card(metrics, 2, "校准", self.calibration_value_var, "")

        hand_section = ttk.Frame(dashboard, style="Panel.TFrame", padding=6)
        hand_section.grid(row=1, column=0, sticky="nsew", padx=(0, 4))
        hand_section.columnconfigure(0, weight=1)
        hand_section.rowconfigure(5, weight=1)
        ttk.Label(hand_section, text="手牌", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        for index in range(4):
            vars_for_card = {
                "title": tk.StringVar(value=f"手牌 {index + 1}"),
                "name": tk.StringVar(value="未识别"),
                "state": tk.StringVar(value="等待"),
                "confidence": tk.StringVar(value="--"),
            }
            self.hand_card_vars.append(vars_for_card)
            self.hand_status_labels.append(self._build_status_card(hand_section, index + 1, vars_for_card))

        skill_section = ttk.Frame(dashboard, style="Panel.TFrame", padding=6)
        skill_section.grid(row=1, column=1, sticky="nsew", padx=(4, 0))
        skill_section.columnconfigure(0, weight=1)
        skill_section.rowconfigure(5, weight=1)
        ttk.Label(skill_section, text="技能", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        for index, skill_id in enumerate(("damage_boost", "cover_tank", "multi_target_fire", "defense_intel_sync"), start=1):
            vars_for_skill = {
                "title": tk.StringVar(value=skill_id),
                "name": tk.StringVar(value=skill_id),
                "state": tk.StringVar(value="等待"),
                "confidence": tk.StringVar(value="--"),
            }
            self.skill_card_vars[skill_id] = vars_for_skill
            self.skill_status_labels[skill_id] = self._build_status_card(skill_section, index, vars_for_skill)

        self.summary_var = self.last_action_var
        self.preview_label = ttk.Label(dashboard)
        self.output_text = tk.Text(self, wrap="none", height=1)

        self.refresh_windows_clicked()
        self._enable_default_automation()
        self.status_var.set("就绪")

    def _build_deck_selector(self) -> None:
        parent = self.deck_selector_frame
        if parent is None:
            return
        for child in parent.winfo_children():
            child.destroy()
        self.deck_card_buttons = []
        previous = {card_id: var.get() for card_id, var in self.deck_card_vars.items()}
        self.deck_card_vars = {}
        self.deck_card_order = []

        panel = ttk.Frame(parent, style="Panel.TFrame", padding=(8, 6))
        panel.grid(row=0, column=0, sticky="ew")
        panel.columnconfigure(0, weight=1)

        header = ttk.Frame(panel, style="Panel.TFrame")
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(1, weight=1)
        ttk.Label(header, text="战前卡组", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(header, textvariable=self.deck_selected_count_var, style="DeckMeta.TLabel").grid(
            row=0,
            column=1,
            sticky="e",
            padx=(8, 8),
        )
        ttk.Button(header, text="全选", command=self._select_all_deck_cards, style="Compact.TButton").grid(
            row=0,
            column=2,
            padx=(0, 4),
        )
        ttk.Button(header, text="清空", command=self._clear_deck_cards, style="Compact.TButton").grid(row=0, column=3)

        cards = self._deck_card_catalog()
        if not cards:
            self.deck_selected_count_var.set("未找到手牌标题模板")
            ttk.Label(panel, text="未找到可选择的手牌模板", style="DeckMeta.TLabel").grid(
                row=1,
                column=0,
                sticky="w",
                pady=(4, 0),
            )
            return

        grid = ttk.Frame(panel, style="Panel.TFrame")
        grid.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        columns = 3
        for column in range(columns):
            grid.columnconfigure(column, weight=1, uniform="deck")

        for index, item in enumerate(cards):
            card_id = str(item["id"])
            var = tk.BooleanVar(value=previous.get(card_id, True))
            var.trace_add("write", lambda *_args: self._update_deck_selected_count())
            self.deck_card_vars[card_id] = var
            self.deck_card_order.append(card_id)
            text = str(item["name"])
            button = tk.Checkbutton(
                grid,
                text=text,
                variable=var,
                anchor="w",
                bg="#f8fafc",
                activebackground="#f8fafc",
                fg="#0f172a",
                selectcolor="#ffffff",
                font=("Microsoft YaHei UI", 8),
                padx=2,
                pady=1,
                wraplength=170,
            )
            button.grid(row=index // columns, column=index % columns, sticky="ew", padx=(0, 6), pady=1)
            self.deck_card_buttons.append(button)
        self._update_deck_selected_count()

    def _deck_card_catalog(self) -> list[dict[str, Any]]:
        try:
            config = self._load_config()
            templates = ScreenReader(config)._get_card_title_templates()
        except Exception as exc:
            self.logger.event("deck_selector_load_error", {"error": str(exc)})
            return []
        cards: list[dict[str, Any]] = []
        for card_id, card in config.cards.items():
            template_paths = templates.get(card_id, [])
            if not template_paths:
                continue
            cards.append(
                {
                    "id": card_id,
                    "name": card.name,
                    "template_count": len(template_paths),
                }
            )
        return cards

    def _selected_deck_card_ids(self) -> list[str]:
        selected: list[str] = []
        for card_id in self.deck_card_order:
            var = self.deck_card_vars.get(card_id)
            if var is not None and var.get():
                selected.append(card_id)
        return selected

    def _update_deck_selected_count(self) -> None:
        selected = len(self._selected_deck_card_ids())
        total = len(self.deck_card_order)
        self.deck_selected_count_var.set(f"已选 {selected}/{total}")

    def _select_all_deck_cards(self) -> None:
        for var in self.deck_card_vars.values():
            var.set(True)
        self._update_deck_selected_count()

    def _clear_deck_cards(self) -> None:
        for var in self.deck_card_vars.values():
            var.set(False)
        self._update_deck_selected_count()

    def _set_deck_selector_running(self, running: bool) -> None:
        state = tk.DISABLED if running else tk.NORMAL
        for button in self.deck_card_buttons:
            button.configure(state=state)
        if self.deck_selector_frame is None:
            return
        if running:
            self.deck_selector_frame.grid_remove()
        else:
            self.deck_selector_frame.grid()

    def _require_selected_deck(self) -> list[str] | None:
        selected = self._selected_deck_card_ids()
        if selected:
            return selected
        self.logger.event("deck_selection_empty")
        messagebox.showinfo("需要卡组", "请至少选择一张手牌模板后再开始识别。")
        return None

    def _apply_selected_deck(self, config: BotConfig) -> BotConfig:
        selected = self._active_deck_card_ids if self._active_deck_card_ids is not None else self._selected_deck_card_ids()
        config.matcher["active_card_ids"] = list(selected)
        return config

    def _deck_selection_payload(self, config: BotConfig | None = None) -> dict[str, Any]:
        selected = self._active_deck_card_ids if self._active_deck_card_ids is not None else self._selected_deck_card_ids()
        names: dict[str, str] = {}
        if config is not None:
            names = {card_id: config.cards[card_id].name for card_id in selected if card_id in config.cards}
        return {
            "card_ids": list(selected),
            "card_count": len(selected),
            "card_names": names,
        }

    def _build_styles(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("App.TFrame", background="#0f172a")
        style.configure("Top.TFrame", background="#e5edf7")
        style.configure("Panel.TFrame", background="#f8fafc", relief="flat")
        style.configure("Metric.TFrame", background="#f8fafc", relief="flat")
        style.configure("Card.TFrame", background="#ffffff", relief="flat")
        style.configure("Title.TLabel", background="#e5edf7", foreground="#0f172a", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("Section.TLabel", background="#f8fafc", foreground="#0f172a", font=("Microsoft YaHei UI", 9, "bold"))
        style.configure("MetricLabel.TLabel", background="#f8fafc", foreground="#64748b", font=("Microsoft YaHei UI", 7))
        style.configure("MetricValue.TLabel", background="#f8fafc", foreground="#0f172a", font=("Microsoft YaHei UI", 16, "bold"))
        style.configure("MetricUnit.TLabel", background="#f8fafc", foreground="#64748b", font=("Microsoft YaHei UI", 8))
        style.configure("CardTitle.TLabel", background="#ffffff", foreground="#64748b", font=("Microsoft YaHei UI", 7))
        style.configure("CardName.TLabel", background="#ffffff", foreground="#0f172a", font=("Microsoft YaHei UI", 8, "bold"))
        style.configure("CardState.TLabel", background="#ffffff", foreground="#16a34a", font=("Microsoft YaHei UI", 8, "bold"))
        style.configure("CardStateReady.TLabel", background="#ffffff", foreground="#16a34a", font=("Microsoft YaHei UI", 8, "bold"))
        style.configure("CardStateMuted.TLabel", background="#ffffff", foreground="#64748b", font=("Microsoft YaHei UI", 8, "bold"))
        style.configure("CardStateWarn.TLabel", background="#ffffff", foreground="#d97706", font=("Microsoft YaHei UI", 8, "bold"))
        style.configure("CardStateDanger.TLabel", background="#ffffff", foreground="#dc2626", font=("Microsoft YaHei UI", 8, "bold"))
        style.configure("CardMeta.TLabel", background="#ffffff", foreground="#64748b", font=("Microsoft YaHei UI", 7))
        style.configure("DeckMeta.TLabel", background="#f8fafc", foreground="#64748b", font=("Microsoft YaHei UI", 7))
        style.configure("Accent.TButton", font=("Microsoft YaHei UI", 8, "bold"))
        style.configure("Compact.TButton", font=("Microsoft YaHei UI", 7))

    def _build_metric_card(
        self,
        parent: ttk.Frame,
        column: int,
        label: str,
        value_var: tk.StringVar,
        unit: str,
    ) -> None:
        card = ttk.Frame(parent, style="Metric.TFrame", padding=(10, 5))
        card.grid(row=0, column=column, sticky="ew", padx=(0, 4) if column == 0 else (4, 0))
        card.columnconfigure(0, weight=1)
        ttk.Label(card, text=label, style="MetricLabel.TLabel").grid(row=0, column=0, sticky="w")
        value_line = ttk.Frame(card, style="Metric.TFrame")
        value_line.grid(row=1, column=0, sticky="w")
        ttk.Label(value_line, textvariable=value_var, style="MetricValue.TLabel").pack(side="left")
        if unit:
            ttk.Label(value_line, text=unit, style="MetricUnit.TLabel").pack(side="left", padx=(3, 0), pady=(8, 0))

    def _build_status_card(
        self,
        parent: ttk.Frame,
        row: int,
        vars_for_card: dict[str, tk.StringVar],
    ) -> ttk.Label:
        card = ttk.Frame(parent, style="Card.TFrame", padding=(7, 4))
        card.grid(row=row, column=0, sticky="ew", pady=(4, 0))
        card.columnconfigure(0, weight=1)
        ttk.Label(card, textvariable=vars_for_card["title"], style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(card, textvariable=vars_for_card["name"], style="CardName.TLabel").grid(row=1, column=0, sticky="w")
        bottom = ttk.Frame(card, style="Card.TFrame")
        bottom.grid(row=2, column=0, sticky="ew", pady=(1, 0))
        bottom.columnconfigure(1, weight=1)
        status_label = ttk.Label(bottom, textvariable=vars_for_card["state"], style="CardStateMuted.TLabel")
        status_label.grid(row=0, column=0, sticky="w")
        ttk.Label(bottom, textvariable=vars_for_card["confidence"], style="CardMeta.TLabel").grid(row=0, column=1, sticky="e")
        return status_label

    def _enable_default_automation(self) -> None:
        self.live_play_card_var.set(True)
        self.live_skill_var.set(True)

    def _browse_config(self) -> None:
        path = filedialog.askopenfilename(
            title="选择配置",
            filetypes=[("JSON config", "*.json"), ("All files", "*.*")],
            initialdir=str(Path("configs").resolve()),
        )
        if path:
            self.config_path_var.set(path)
            self.logger.event("config_selected", {"path": path})
            self._build_deck_selector()

    def _browse_image(self) -> None:
        path = filedialog.askopenfilename(
            title="选择截图",
            filetypes=[
                ("Images", "*.png;*.jpg;*.jpeg;*.bmp;*.webp"),
                ("All files", "*.*"),
            ],
            initialdir=str(Path("screenshots").resolve()),
        )
        if path:
            self.image_path_var.set(path)
            self.logger.event("image_selected", {"path": path})
            self._show_plain_image(Path(path))

    def _show_plain_image(self, path: Path) -> None:
        try:
            self._display_image(Image.open(path).convert("RGB"))
            self.status_var.set(f"Loaded image: {path}")
            self.logger.event("image_loaded", {"path": str(path)})
        except Exception as exc:
            self.logger.event("image_load_error", {"path": str(path), "error": str(exc)})
            messagebox.showerror("图片错误", str(exc))

    def _on_close(self) -> None:
        self._stop_hand_training("close", stop_continuous=False)
        self._stop_battle_training("close")
        self._stop_continuous_window("close")
        stop_wgc_sessions()
        self._clear_capture_window_topmost()
        self.logger.event("session_close")
        self.destroy()

    def _load_config(self) -> BotConfig:
        return BotConfig.load(Path(self.config_path_var.get()))

    def _continuous_screen_options(self, config: BotConfig | None = None) -> dict[str, Any]:
        try:
            config = config or self._load_config()
            screen = config.screen
        except Exception:
            screen = {}

        target_ms = max(1, int(screen.get("continuous_target_frame_ms", CONTINUOUS_TARGET_FRAME_MS)))
        min_delay_ms = max(0, int(screen.get("continuous_min_delay_ms", CONTINUOUS_MIN_DELAY_MS)))
        max_delay_ms = max(min_delay_ms, int(screen.get("continuous_max_delay_ms", CONTINUOUS_MAX_DELAY_MS)))
        save_every_n = max(0, int(screen.get("continuous_save_every_n", 0)))
        preview_every_n = max(1, int(screen.get("continuous_preview_every_n", 1)))
        return {
            "target_frame_ms": target_ms,
            "min_delay_ms": min_delay_ms,
            "max_delay_ms": max_delay_ms,
            "save_images": bool(screen.get("continuous_save_images", False)),
            "save_every_n": save_every_n,
            "allow_inline_training": bool(screen.get("continuous_allow_inline_training", False)),
            "preview_every_n": preview_every_n,
        }

    def _adaptive_continuous_delay_ms(self, processing_ms: float | None) -> int:
        options = self._continuous_screen_options()
        if processing_ms is None:
            return int(options["min_delay_ms"])
        delay_ms = int(round(float(options["target_frame_ms"]) - max(0.0, processing_ms)))
        return max(int(options["min_delay_ms"]), min(int(options["max_delay_ms"]), delay_ms))

    def _should_save_detection_images(self, config: BotConfig, frame_index: int | None) -> bool:
        if not self.continuous_window_var.get():
            return True
        options = self._continuous_screen_options(config)
        if options["save_images"]:
            return True
        save_every_n = int(options["save_every_n"])
        return save_every_n > 0 and frame_index is not None and frame_index % save_every_n == 0

    def _parse_time_override(self) -> float | None:
        phase = self.phase_override_var.get().strip()
        if not phase or phase == "auto":
            return None
        if phase == "placement":
            return 76.0
        if phase == "extra_place":
            return 125.0
        if phase == "battle":
            return 140.0
        text = self.time_var.get().strip()
        if not text:
            return None
        return float(text)

    def _apply_cost_override(self, state: GameState) -> None:
        if not self.cost_override_enabled.get():
            return
        text = self.cost_override_var.get().strip()
        if text:
            state.cost = int(text)

    def _apply_phase_override(self, state: GameState) -> str | None:
        phase = self.phase_override_var.get().strip()
        if not phase or phase == "auto":
            return None
        state.phase = Phase(phase)
        return phase

    def _phase_override_value(self) -> Phase | None:
        phase = self.phase_override_var.get().strip()
        if not phase or phase == "auto":
            return None
        return Phase(phase)

    def _run_worker(self, label: str, fn, *args, allow_when_busy: bool = False) -> bool:
        if self._worker_busy and not allow_when_busy:
            self.logger.event("worker_skipped_busy", {"label": label})
            return False
        self._worker_busy = True
        self.status_var.set(f"{label}...")
        self.logger.event(
            "worker_start",
            {
                "label": label,
                "args": [str(arg) for arg in args],
                "config_path": self.config_path_var.get(),
                "time_override": self._parse_time_override(),
                "phase_override": self.phase_override_var.get(),
                "cost_override_enabled": self.cost_override_enabled.get(),
                "cost_override": self.cost_override_var.get(),
                "active_deck": self._deck_selection_payload(),
            },
        )
        thread = threading.Thread(target=self._worker_entry, args=(label, fn, args), daemon=True)
        thread.start()
        return True

    def _worker_entry(self, label: str, fn, args: tuple[Any, ...]) -> None:
        try:
            self._queue.put(("result", {"label": label, "result": fn(*args)}))
        except Exception:
            self._queue.put(("error", {"label": label, "traceback": traceback.format_exc()}))

    def validate_assets_clicked(self) -> None:
        self.logger.event("validate_assets_clicked")
        self._run_worker("校验素材", self._validate_assets_worker)

    def _validate_assets_worker(self) -> tuple[str, dict[str, Any], None]:
        config = self._load_config()
        report = validate_assets(config)
        return "assets", report, None

    def auto_calibrate_clicked(self) -> None:
        try:
            window = self._continuous_window if self.continuous_window_var.get() else self._selected_window()
            if window is None:
                window = self._selected_window()
        except Exception as exc:
            self.logger.event("auto_calibration_select_error", {"error": str(exc)})
            messagebox.showinfo("需要窗口", str(exc))
            return
        if self.continuous_window_var.get():
            self._stop_continuous_window("auto_calibration")
        self.calibration_value_var.set("进行中")
        self.logger.event(
            "auto_calibration_clicked",
            {
                "hwnd": window.hwnd,
                "title": window.title,
                "client_capture_rect": window.client_capture_rect,
            },
        )
        if not self._run_worker("自动校准", self._auto_calibration_worker, window):
            self.calibration_value_var.set("忙碌")

    def _auto_calibration_worker(self, window: WindowInfo) -> tuple[str, dict[str, Any], Image.Image | None]:
        config = self._load_config()
        window = get_window(window.hwnd)
        focus_window(window.hwnd)
        frame_count = max(1, int(config.screen.get("calibration_sample_frames", 4)))
        interval_ms = max(0, int(config.screen.get("calibration_sample_interval_ms", 250)))
        stable_required = max(1, int(config.screen.get("calibration_stable_required_frames", min(2, frame_count))))
        stable_tolerance = max(0, int(config.screen.get("calibration_stable_tolerance_px", 4)))

        samples: list[dict[str, Any]] = []
        sample_images: dict[int, Image.Image] = {}
        for frame_index in range(1, frame_count + 1):
            window = get_window(window.hwnd)
            capture_started = time.perf_counter()
            image, origin, monitor = capture_window(window, config)
            capture_elapsed_ms = round((time.perf_counter() - capture_started) * 1000, 1)
            image, scale = normalize_capture_image(image, config)
            raw_path = self.logger.save_image(f"calibration_raw_{frame_index}_{window.title}", image)
            result = calibrate_layout_offset(config, image)
            samples.append(
                {
                    "frame_index": frame_index,
                    "ok": bool(result.get("ok")),
                    "result": result,
                    "raw_image": str(raw_path.resolve()),
                    "source": {
                        "hwnd": window.hwnd,
                        "title": window.title,
                        "client_capture_rect": window.client_capture_rect,
                        "capture_monitor": monitor,
                        "capture_origin": origin,
                        "capture_scale": scale,
                        "capture_ms": capture_elapsed_ms,
                    },
                }
            )
            sample_images[frame_index] = image
            if frame_index < frame_count and interval_ms > 0:
                time.sleep(interval_ms / 1000.0)

        selected_sample, stability = _select_stable_calibration_sample(
            samples,
            required_count=stable_required,
            tolerance=stable_tolerance,
        )
        selected_sample = selected_sample or samples[-1]
        result = selected_sample.get("result", {})
        source = selected_sample.get("source", {})
        image = sample_images.get(int(selected_sample.get("frame_index", 0))) or next(iter(sample_images.values()))
        final_ok = bool(selected_sample.get("ok")) and bool(stability.get("ok"))
        calibration = {
            "offset_x": int(result.get("offset_x", 0)),
            "offset_y": int(result.get("offset_y", 0)),
            "score": float(result.get("score", 0.0)),
            "min_score": float(result.get("min_score", 0.0)),
            "source": "hand_bar_scan_multi_sample",
            "sample_count": frame_count,
            "stable_count": int(stability.get("stable_count", 0)),
            "stable_required": stable_required,
            "stable_tolerance_px": stable_tolerance,
            "client_size": [
                int(source.get("client_capture_rect", window.client_capture_rect)[2]),
                int(source.get("client_capture_rect", window.client_capture_rect)[3]),
            ],
            "capture_scale": list(source.get("capture_scale", (1.0, 1.0))),
        }
        saved_path: Path | None = None
        if final_ok:
            saved_path = save_local_calibration(config, calibration)

        annotated = image.convert("RGB").copy()
        draw = ImageDraw.Draw(annotated)
        for slot in config.hand_slots:
            shifted = _offset_rect(
                slot.rect,
                int(result.get("offset_x", 0)),
                int(result.get("offset_y", 0)),
            )
            draw.rectangle(_rect_xyxy(shifted), outline="#22c55e" if final_ok else "#ef4444", width=3)
            draw.text((shifted[0] + 4, shifted[1] + 4), slot.name, fill="#22c55e" if final_ok else "#ef4444")
        annotated_path = self.logger.save_image(f"calibration_annotated_{window.title}", annotated)
        payload = {
            "ok": final_ok,
            "result": result,
            "stability": stability,
            "samples": samples,
            "calibration": calibration,
            "saved_path": str(saved_path.resolve()) if saved_path else None,
            "raw_image": selected_sample.get("raw_image"),
            "annotated_image": str(annotated_path.resolve()),
            "source": source,
        }
        return "calibration", payload, annotated

    def detect_image_clicked(self) -> None:
        image_path = self.image_path_var.get().strip()
        self.logger.event("detect_image_clicked", {"image_path": image_path})
        if not image_path:
            messagebox.showinfo("需要截图", "请先选择一张截图。")
            return
        self._run_worker("识别截图", self._detect_worker, Path(image_path), False)

    def capture_clicked(self) -> None:
        self.logger.event("capture_clicked")
        self._run_worker("截屏识别", self._detect_worker, None, True)

    def refresh_windows_clicked(self) -> None:
        try:
            self._windows = list_visible_windows()
            labels = [
                f"{item.title}  [{item.client_capture_rect[2]}x{item.client_capture_rect[3]} hwnd={item.hwnd}]"
                for item in self._windows
            ]
            self.window_combo.configure(values=labels)
            if labels and not self.window_var.get():
                lagrange_index = next(
                    (idx for idx, item in enumerate(self._windows) if "拉格朗日" in item.title or "lagrange" in item.title.casefold()),
                    0,
                )
                self.window_combo.current(lagrange_index)
            self.status_var.set(f"窗口列表已刷新: {len(labels)} 个")
            self.logger.event(
                "windows_refreshed",
                {
                    "selected_index": self.window_combo.current(),
                    "windows": [
                        {
                            "hwnd": item.hwnd,
                            "title": item.title,
                            "rect": item.rect,
                            "client_rect": item.client_rect,
                            "client_capture_rect": item.client_capture_rect,
                        }
                        for item in self._windows
                    ],
                },
            )
        except Exception as exc:
            self.status_var.set("窗口列表刷新失败")
            self.logger.event("windows_refresh_error", {"error": str(exc)})
            messagebox.showerror("窗口错误", str(exc))

    def _selected_window(self) -> WindowInfo:
        index = self.window_combo.current()
        if index < 0 or index >= len(self._windows):
            raise RuntimeError("请先选择一个窗口")
        return self._windows[index]

    def detect_window_clicked(self) -> None:
        if not self._require_selected_deck():
            return
        try:
            window = self._selected_window()
        except Exception as exc:
            self.logger.event("detect_window_select_error", {"error": str(exc)})
            messagebox.showinfo("需要窗口", str(exc))
            return
        self.logger.event(
            "detect_window_clicked",
            {
                "hwnd": window.hwnd,
                "title": window.title,
                "rect": window.rect,
                "client_rect": window.client_rect,
                "client_capture_rect": window.client_capture_rect,
            },
        )
        self._run_worker("窗口识别", self._detect_window_worker, window)

    def toggle_continuous_window_clicked(self) -> None:
        if self.continuous_window_var.get():
            self._stop_continuous_window("button")
            return

        self._enable_default_automation()
        selected_deck = self._require_selected_deck()
        if selected_deck is None:
            return
        self._active_deck_card_ids = selected_deck
        try:
            window = self._selected_window()
        except Exception as exc:
            self._active_deck_card_ids = None
            self.logger.event("continuous_window_select_error", {"error": str(exc)})
            messagebox.showinfo("需要窗口", str(exc))
            return

        self._start_continuous_window(window, trigger="button")

    def _start_continuous_window(self, window: WindowInfo, trigger: str) -> None:
        self._continuous_window = window
        self._continuous_reader = None
        self._continuous_count = 0
        self._continuous_last_processing_ms = None
        self._continuous_last_delay_ms = 0
        self._last_continuous_capture_rect = None
        self._last_continuous_rect_change_monotonic = None
        self._opening_zoom_done = False
        self._opening_zoom_active = False
        stop_wgc_sessions()
        self._set_capture_window_topmost(window)
        self.continuous_window_var.set(True)
        self._set_deck_selector_running(True)
        self.continuous_button.configure(text="停止识别")
        options = self._continuous_screen_options()
        self.logger.event(
            "continuous_window_start",
            {
                "interval_ms": CONTINUOUS_CAPTURE_INTERVAL_MS,
                "target_frame_ms": options["target_frame_ms"],
                "min_delay_ms": options["min_delay_ms"],
                "max_delay_ms": options["max_delay_ms"],
                "save_images": options["save_images"],
                "save_every_n": options["save_every_n"],
                "preview_every_n": options["preview_every_n"],
                "active_deck": self._deck_selection_payload(),
                "trigger": trigger,
                "hwnd": window.hwnd,
                "title": window.title,
                "rect": window.rect,
                "client_rect": window.client_rect,
                "client_capture_rect": window.client_capture_rect,
            },
        )
        self._schedule_continuous_window(0)

    def _stop_continuous_window(self, reason: str) -> None:
        if not self.continuous_window_var.get():
            return
        if self._hand_training_active:
            self._stop_hand_training(f"continuous_{reason}", stop_continuous=False)
        self.continuous_window_var.set(False)
        if self._continuous_after_id is not None:
            try:
                self.after_cancel(self._continuous_after_id)
            except Exception:
                pass
            self._continuous_after_id = None
        self._continuous_window = None
        self._continuous_reader = None
        self._active_deck_card_ids = None
        self._continuous_last_processing_ms = None
        self._continuous_last_delay_ms = 0
        self._last_continuous_capture_rect = None
        self._last_continuous_rect_change_monotonic = None
        self._opening_zoom_active = False
        self.continuous_button.configure(text="开始识别")
        self._set_deck_selector_running(False)
        stop_wgc_sessions()
        self._clear_capture_window_topmost()
        self.logger.event(
            "continuous_window_stop",
            {
                "reason": reason,
                "frames_requested": self._continuous_count,
            },
        )
        self.status_var.set(f"连续识别已停止 | log: {self.logger.dir}")

    def toggle_hand_training_clicked(self) -> None:
        if self._hand_training_active:
            self._stop_hand_training("button", stop_continuous=True)
            return

        try:
            window = self._continuous_window if self.continuous_window_var.get() else self._selected_window()
        except Exception as exc:
            self.logger.event("hand_training_select_error", {"error": str(exc)})
            messagebox.showinfo("需要窗口", str(exc))
            return

        self._start_hand_training_process(window)

    def _start_hand_training_process(self, window: WindowInfo) -> None:
        started_at = datetime.now().astimezone()
        training_dir = (Path("training_samples") / f"hand_live_{started_at.strftime('%Y%m%d_%H%M%S_%f')}").resolve()
        training_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = training_dir / "collector.out.log"
        stderr_path = training_dir / "collector.err.log"
        command = [
            sys.executable,
            "-m",
            "lagrange_bot.bot",
            "collect-training",
            "--config",
            self.config_path_var.get(),
            "--hwnd",
            str(window.hwnd),
            "--output",
            str(training_dir),
            "--interval-ms",
            "1000",
            "--duration",
            "120",
        ]
        stdout_handle = stdout_path.open("a", encoding="utf-8")
        stderr_handle = stderr_path.open("a", encoding="utf-8")
        try:
            self._set_capture_window_topmost(window)
        except Exception:
            pass
        try:
            process = subprocess.Popen(
                command,
                cwd=str(Path.cwd()),
                stdout=stdout_handle,
                stderr=stderr_handle,
                text=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as exc:
            stdout_handle.close()
            stderr_handle.close()
            self.logger.event("hand_training_process_start_error", {"error": str(exc), "command": command})
            messagebox.showerror("Hand training", str(exc))
            return
        with self._hand_training_lock:
            self._hand_training_active = True
            self.hand_training_var.set(True)
            self._hand_training_started_continuous = False
            self._hand_training_count = 0
            self._hand_training_dir = training_dir
            self._hand_training_manifest_path = training_dir / "manifest.jsonl"
            self._hand_training_external = True
            self._hand_training_process = process
            self._hand_training_stdout = stdout_handle
            self._hand_training_stderr = stderr_handle
        self.hand_training_button.configure(text="停止手牌采集")
        self.logger.event(
            "hand_training_process_start",
            {
                "pid": process.pid,
                "dir": str(training_dir),
                "manifest": str((training_dir / "manifest.jsonl").resolve()),
                "stdout": str(stdout_path),
                "stderr": str(stderr_path),
                "command": command,
                "hwnd": window.hwnd,
                "title": window.title,
            },
        )
        self.status_var.set(f"手牌采集进程运行中 pid={process.pid} | {training_dir}")
        self.after(1000, self._poll_hand_training_process)

    def _start_hand_training(self, window: WindowInfo) -> None:
        started_at = datetime.now().astimezone()
        training_dir = Path("training_samples") / f"hand_live_{started_at.strftime('%Y%m%d_%H%M%S_%f')}"
        for child in ("raw", "slots", "titles", "timer", "command"):
            (training_dir / child).mkdir(parents=True, exist_ok=True)
        manifest_path = training_dir / "manifest.jsonl"
        with self._hand_training_lock:
            self._hand_training_active = True
            self.hand_training_var.set(True)
            self._hand_training_count = 0
            self._hand_training_dir = training_dir
            self._hand_training_manifest_path = manifest_path
        self.hand_training_button.configure(text="停止手牌采集")
        self.logger.event(
            "hand_training_start",
            {
                "dir": str(training_dir.resolve()),
                "manifest": str(manifest_path.resolve()),
                "interval_ms": CONTINUOUS_CAPTURE_INTERVAL_MS,
                "started_continuous": self._hand_training_started_continuous,
                "hwnd": window.hwnd,
                "title": window.title,
                "client_capture_rect": window.client_capture_rect,
            },
        )
        self.status_var.set(f"手牌采集中 | {training_dir}")

    def _stop_hand_training(self, reason: str, stop_continuous: bool) -> None:
        with self._hand_training_lock:
            if not self._hand_training_active:
                return
            training_dir = self._hand_training_dir
            manifest_path = self._hand_training_manifest_path
            frames = self._hand_training_count
            started_continuous = self._hand_training_started_continuous
            process = self._hand_training_process
            stdout_handle = self._hand_training_stdout
            stderr_handle = self._hand_training_stderr
            external = self._hand_training_external
            self._hand_training_active = False
            self.hand_training_var.set(False)
            self._hand_training_started_continuous = False
            self._hand_training_external = False
            self._hand_training_process = None
            self._hand_training_stdout = None
            self._hand_training_stderr = None
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3.0)
        for handle in (stdout_handle, stderr_handle):
            if handle is not None:
                handle.close()
        self.hand_training_button.configure(text="开始手牌采集")
        self.logger.event(
            "hand_training_stop",
            {
                "reason": reason,
                "frames_saved": frames,
                "dir": str(training_dir.resolve()) if training_dir else None,
                "manifest": str(manifest_path.resolve()) if manifest_path else None,
                "process_returncode": process.poll() if process is not None else None,
            },
        )
        self.status_var.set(f"手牌采集已停止, frames={frames} | {training_dir}")
        if stop_continuous and started_continuous and self.continuous_window_var.get():
            self._stop_continuous_window(f"hand_training_{reason}")
        if external:
            self._clear_capture_window_topmost()

    def _poll_hand_training_process(self) -> None:
        with self._hand_training_lock:
            active = self._hand_training_active
            process = self._hand_training_process
            training_dir = self._hand_training_dir
            manifest_path = self._hand_training_manifest_path
            stdout_handle = self._hand_training_stdout
            stderr_handle = self._hand_training_stderr
        if not active or process is None:
            return
        returncode = process.poll()
        if returncode is None:
            self.after(1000, self._poll_hand_training_process)
            return
        frames = 0
        if manifest_path and manifest_path.exists():
            try:
                frames = sum(1 for _ in manifest_path.open("r", encoding="utf-8"))
            except Exception:
                frames = 0
        for handle in (stdout_handle, stderr_handle):
            if handle is not None:
                handle.close()
        with self._hand_training_lock:
            self._hand_training_active = False
            self.hand_training_var.set(False)
            self._hand_training_process = None
            self._hand_training_stdout = None
            self._hand_training_stderr = None
            self._hand_training_external = False
            self._hand_training_count = frames
        self.hand_training_button.configure(text="开始手牌采集")
        self.logger.event(
            "hand_training_process_exit",
            {
                "returncode": returncode,
                "frames_saved": frames,
                "dir": str(training_dir) if training_dir else None,
                "manifest": str(manifest_path) if manifest_path else None,
            },
        )
        self.status_var.set(f"手牌采集已结束 frames={frames} | {training_dir}")
        if self._topmost_hwnd is not None:
            self._clear_capture_window_topmost()

    def toggle_battle_training_clicked(self) -> None:
        if self._battle_training_active:
            self._stop_battle_training("button")
            return

        try:
            window = self._continuous_window if self.continuous_window_var.get() else self._selected_window()
        except Exception as exc:
            self.logger.event("battle_training_select_error", {"error": str(exc)})
            messagebox.showinfo("需要窗口", str(exc))
            return

        self._start_battle_training_process(window)

    def _start_battle_training_process(self, window: WindowInfo) -> None:
        started_at = datetime.now().astimezone()
        training_dir = (Path("training_samples") / f"battle_live_{started_at.strftime('%Y%m%d_%H%M%S_%f')}").resolve()
        training_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = training_dir / "collector.out.log"
        stderr_path = training_dir / "collector.err.log"
        command = [
            sys.executable,
            "-m",
            "lagrange_bot.bot",
            "collect-battle",
            "--config",
            self.config_path_var.get(),
            "--hwnd",
            str(window.hwnd),
            "--output",
            str(training_dir),
            "--interval-ms",
            "750",
            "--duration",
            "120",
        ]
        stdout_handle = stdout_path.open("a", encoding="utf-8")
        stderr_handle = stderr_path.open("a", encoding="utf-8")
        try:
            self._set_capture_window_topmost(window)
        except Exception:
            pass
        try:
            process = subprocess.Popen(
                command,
                cwd=str(Path.cwd()),
                stdout=stdout_handle,
                stderr=stderr_handle,
                text=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as exc:
            stdout_handle.close()
            stderr_handle.close()
            self.logger.event("battle_training_process_start_error", {"error": str(exc), "command": command})
            messagebox.showerror("Battle training", str(exc))
            return
        with self._battle_training_lock:
            self._battle_training_active = True
            self.battle_training_var.set(True)
            self._battle_training_dir = training_dir
            self._battle_training_manifest_path = training_dir / "manifest.jsonl"
            self._battle_training_process = process
            self._battle_training_stdout = stdout_handle
            self._battle_training_stderr = stderr_handle
        self.battle_training_button.configure(text="停止战斗采集")
        self.logger.event(
            "battle_training_process_start",
            {
                "pid": process.pid,
                "dir": str(training_dir),
                "manifest": str((training_dir / "manifest.jsonl").resolve()),
                "stdout": str(stdout_path),
                "stderr": str(stderr_path),
                "command": command,
                "hwnd": window.hwnd,
                "title": window.title,
            },
        )
        self.status_var.set(f"战斗采集进程运行中 pid={process.pid} | {training_dir}")
        self.after(1000, self._poll_battle_training_process)

    def _stop_battle_training(self, reason: str) -> None:
        with self._battle_training_lock:
            if not self._battle_training_active:
                return
            training_dir = self._battle_training_dir
            manifest_path = self._battle_training_manifest_path
            process = self._battle_training_process
            stdout_handle = self._battle_training_stdout
            stderr_handle = self._battle_training_stderr
            self._battle_training_active = False
            self.battle_training_var.set(False)
            self._battle_training_process = None
            self._battle_training_stdout = None
            self._battle_training_stderr = None
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3.0)
        for handle in (stdout_handle, stderr_handle):
            if handle is not None:
                handle.close()
        self.battle_training_button.configure(text="开始战斗采集")
        frames = 0
        if manifest_path and manifest_path.exists():
            try:
                frames = sum(1 for _ in manifest_path.open("r", encoding="utf-8"))
            except Exception:
                frames = 0
        self.logger.event(
            "battle_training_stop",
            {
                "reason": reason,
                "frames_saved": frames,
                "dir": str(training_dir.resolve()) if training_dir else None,
                "manifest": str(manifest_path.resolve()) if manifest_path else None,
                "process_returncode": process.poll() if process is not None else None,
            },
        )
        self.status_var.set(f"战斗采集已停止, frames={frames} | {training_dir}")
        self._clear_capture_window_topmost()

    def _poll_battle_training_process(self) -> None:
        with self._battle_training_lock:
            active = self._battle_training_active
            process = self._battle_training_process
            training_dir = self._battle_training_dir
            manifest_path = self._battle_training_manifest_path
            stdout_handle = self._battle_training_stdout
            stderr_handle = self._battle_training_stderr
        if not active or process is None:
            return
        returncode = process.poll()
        if returncode is None:
            self.after(1000, self._poll_battle_training_process)
            return
        frames = 0
        if manifest_path and manifest_path.exists():
            try:
                frames = sum(1 for _ in manifest_path.open("r", encoding="utf-8"))
            except Exception:
                frames = 0
        for handle in (stdout_handle, stderr_handle):
            if handle is not None:
                handle.close()
        with self._battle_training_lock:
            self._battle_training_active = False
            self.battle_training_var.set(False)
            self._battle_training_process = None
            self._battle_training_stdout = None
            self._battle_training_stderr = None
        self.battle_training_button.configure(text="开始战斗采集")
        self.logger.event(
            "battle_training_process_exit",
            {
                "returncode": returncode,
                "frames_saved": frames,
                "dir": str(training_dir) if training_dir else None,
                "manifest": str(manifest_path) if manifest_path else None,
            },
        )
        self.status_var.set(f"战斗采集已结束 frames={frames} | {training_dir}")
        self._clear_capture_window_topmost()

    def _set_capture_window_topmost(self, window: WindowInfo) -> None:
        try:
            focus_window(window.hwnd)
            set_window_topmost(window.hwnd, True)
            self._topmost_hwnd = window.hwnd
            self.logger.event(
                "capture_window_topmost_enabled",
                {"hwnd": window.hwnd, "title": window.title},
            )
        except Exception as exc:
            self.logger.event(
                "capture_window_topmost_error",
                {"hwnd": window.hwnd, "title": window.title, "error": str(exc)},
            )

    def _clear_capture_window_topmost(self) -> None:
        if self._topmost_hwnd is None:
            return
        hwnd = self._topmost_hwnd
        self._topmost_hwnd = None
        try:
            set_window_topmost(hwnd, False)
            self.logger.event("capture_window_topmost_disabled", {"hwnd": hwnd})
        except Exception as exc:
            self.logger.event("capture_window_topmost_disable_error", {"hwnd": hwnd, "error": str(exc)})

    def _maybe_start_opening_zoom(self, config: BotConfig, state: GameState) -> None:
        if not self.continuous_window_var.get() or self._opening_zoom_done or self._opening_zoom_active:
            return
        screen = config.screen
        if not bool(screen.get("opening_zoom_enabled", False)):
            return
        now_seconds = float(state.now_seconds)
        trigger_seconds = float(screen.get("opening_zoom_trigger_seconds", 10.0))
        until_seconds = float(screen.get("opening_zoom_trigger_until_seconds", trigger_seconds + 30.0))
        if now_seconds < trigger_seconds or now_seconds > until_seconds:
            return
        window = self._continuous_window
        if window is None:
            return
        self._opening_zoom_done = True
        self._opening_zoom_active = True
        duration_seconds = max(0.0, float(screen.get("opening_zoom_duration_seconds", 10.0)))
        scroll_amount = int(screen.get("opening_zoom_scroll_amount", -8))
        interval_seconds = max(0.005, float(screen.get("opening_zoom_interval_ms", 25)) / 1000.0)
        thread = threading.Thread(
            target=self._opening_zoom_worker,
            args=(window.hwnd, duration_seconds, scroll_amount, interval_seconds, now_seconds),
            daemon=True,
        )
        thread.start()

    def _opening_zoom_worker(
        self,
        hwnd: int,
        duration_seconds: float,
        scroll_amount: int,
        interval_seconds: float,
        trigger_time_seconds: float,
    ) -> None:
        steps = 0
        try:
            import pyautogui

            window = get_window(hwnd)
            focus_window(hwnd)
            left, top, width, height = window.client_capture_rect
            center = (left + width // 2, top + height // 2)
            pyautogui.moveTo(center[0], center[1])
            self.logger.event(
                "opening_zoom_start",
                {
                    "hwnd": hwnd,
                    "trigger_time_seconds": round(trigger_time_seconds, 2),
                    "duration_seconds": duration_seconds,
                    "scroll_amount": scroll_amount,
                    "interval_seconds": interval_seconds,
                    "center": center,
                },
            )
            deadline = time.monotonic() + duration_seconds
            while self.continuous_window_var.get() and time.monotonic() < deadline:
                pyautogui.scroll(scroll_amount)
                steps += 1
                time.sleep(interval_seconds)
            self.logger.event("opening_zoom_done", {"hwnd": hwnd, "steps": steps})
        except Exception as exc:
            self.logger.event("opening_zoom_error", {"hwnd": hwnd, "steps": steps, "error": str(exc)})
        finally:
            self._opening_zoom_active = False

    def _schedule_continuous_window(self, delay_ms: int = CONTINUOUS_CAPTURE_INTERVAL_MS) -> None:
        if not self.continuous_window_var.get():
            return
        if self._continuous_after_id is not None:
            try:
                self.after_cancel(self._continuous_after_id)
            except Exception:
                pass
        delay_ms = max(0, int(delay_ms))
        self._continuous_after_id = self.after(delay_ms, self._continuous_window_tick)

    def _continuous_window_tick(self) -> None:
        self._continuous_after_id = None
        if not self.continuous_window_var.get():
            return
        window = self._continuous_window
        if window is None:
            self._stop_continuous_window("missing_window")
            return

        try:
            window = get_window(window.hwnd)
        except Exception as exc:
            self.logger.event("continuous_window_refresh_error", {"error": str(exc)})
            self._stop_continuous_window("refresh_error")
            return
        self._continuous_window = window

        if self._continuous_window_rect_is_unstable(window):
            if self.continuous_window_var.get():
                self._schedule_continuous_window(self._adaptive_continuous_delay_ms(None))
            return

        if self._worker_busy:
            delay_ms = self._adaptive_continuous_delay_ms(self._continuous_last_processing_ms)
            self.logger.event(
                "continuous_window_skip_busy",
                {
                    "frame_index": self._continuous_count + 1,
                    "next_delay_ms": delay_ms,
                    "last_processing_ms": self._continuous_last_processing_ms,
                },
            )
            self._schedule_continuous_window(delay_ms)
        else:
            self._continuous_count += 1
            started = self._run_worker(
                f"连续窗口识别 #{self._continuous_count}",
                self._detect_window_worker,
                window,
            )
            if not started and self.continuous_window_var.get():
                self._schedule_continuous_window(self._adaptive_continuous_delay_ms(self._continuous_last_processing_ms))

    def _continuous_window_rect_is_unstable(self, window: WindowInfo) -> bool:
        rect = window.client_capture_rect
        now = time.monotonic()
        settle_seconds = 0.35
        try:
            config = self._load_config()
            settle_seconds = max(0.0, float(config.screen.get("capture_settle_after_move_seconds", settle_seconds)))
        except Exception:
            pass

        if self._last_continuous_capture_rect is None:
            self._last_continuous_capture_rect = rect
            return False

        if rect != self._last_continuous_capture_rect:
            previous = self._last_continuous_capture_rect
            self._last_continuous_capture_rect = rect
            self._last_continuous_rect_change_monotonic = now
            self.logger.event(
                "continuous_window_skip_unstable_rect",
                {
                    "reason": "rect_changed",
                    "previous_client_capture_rect": previous,
                    "client_capture_rect": rect,
                    "settle_seconds": settle_seconds,
                },
            )
            return True

        changed_at = self._last_continuous_rect_change_monotonic
        if changed_at is not None and now - changed_at < settle_seconds:
            self.logger.event(
                "continuous_window_skip_unstable_rect",
                {
                    "reason": "settling",
                    "client_capture_rect": rect,
                    "elapsed_seconds": round(now - changed_at, 3),
                    "settle_seconds": settle_seconds,
                },
            )
            return True

        return False

    def _detect_window_worker(self, window: WindowInfo) -> tuple[str, dict[str, Any], Image.Image | None]:
        config = self._apply_selected_deck(self._load_config())
        window = get_window(window.hwnd)
        if self.continuous_window_var.get():
            if self._continuous_reader is None:
                self._continuous_reader = ScreenReader(config)
            reader = self._continuous_reader
            reader.config = config
        else:
            reader = ScreenReader(config)
        focus_window(window.hwnd)
        capture_started = time.perf_counter()
        image, origin, monitor = capture_window(window, config)
        capture_elapsed_ms = round((time.perf_counter() - capture_started) * 1000, 1)
        self.logger.event(
            "window_captured",
            {
                "hwnd": window.hwnd,
                "title": window.title,
                "client_capture_rect": window.client_capture_rect,
                "capture_monitor": monitor,
                "capture_origin": origin,
                "image_size": image.size,
                "timings": {"capture_ms": capture_elapsed_ms},
            },
        )
        image, scale = normalize_capture_image(image, config)
        return self._detect_image_object(
            image,
            origin,
            window.title,
            capture_scale=scale,
            config=config,
            reader=reader,
            timings={"capture_ms": capture_elapsed_ms},
        )

    def _detect_worker(self, image_path: Path | None, capture: bool) -> tuple[str, dict[str, Any], Image.Image | None]:
        config = self._apply_selected_deck(self._load_config())
        reader = ScreenReader(config)
        if capture:
            image = reader.capture()
            origin = reader.last_capture_origin
            scale = reader.last_capture_scale
            source = "screen"
            self.logger.event(
                "screen_captured",
                {
                    "origin": origin,
                    "scale": scale,
                    "image_size": image.size,
                    "window_title_contains": config.screen.get("window_title_contains"),
                },
            )
        else:
            assert image_path is not None
            image = Image.open(image_path).convert("RGB")
            origin = (0, 0)
            scale = (1.0, 1.0)
            source = str(image_path)
            self.logger.event(
                "image_opened_for_detection",
                {
                    "path": str(image_path),
                    "image_size": image.size,
                },
            )
        return self._detect_image_object(image, origin, source, capture_scale=scale, config=config, reader=reader)

    def _detect_image_object(
        self,
        image: Image.Image,
        origin: tuple[int, int],
        source: str,
        capture_scale: tuple[float, float] = (1.0, 1.0),
        config: BotConfig | None = None,
        reader: ScreenReader | None = None,
        timings: dict[str, float] | None = None,
    ) -> tuple[str, dict[str, Any], Image.Image | None]:
        config = config or self._load_config()
        config = self._apply_selected_deck(config)
        reader = reader or ScreenReader(config)
        timings = dict(timings or {})
        total_started = time.perf_counter()
        continuous_active = self.continuous_window_var.get()
        frame_index = self._continuous_count if continuous_active else None
        continuous_options = self._continuous_screen_options(config) if continuous_active else None
        read_started = time.perf_counter()
        phase_override_value = self._phase_override_value()
        time_override = self._parse_time_override()
        state = reader.read_state_from_image(
            image,
            now_seconds=time_override,
            phase_override=phase_override_value,
            capture_origin=origin,
            capture_scale=capture_scale,
        )
        timings["read_state_ms"] = round((time.perf_counter() - read_started) * 1000, 1)
        self._apply_cost_override(state)
        live_skill_pending = self._update_pending_live_skill(reader, state, config, continuous_active)
        phase_override = phase_override_value.value if phase_override_value is not None else None
        self._maybe_start_opening_zoom(config, state)
        decision_started = time.perf_counter()
        action = DeckPolicy(config).decide(state)
        timings["decision_ms"] = round((time.perf_counter() - decision_started) * 1000, 1)
        annotate_started = time.perf_counter()
        should_update_preview = self._should_update_preview(config, frame_index)
        annotated = (
            annotate_image(image, config, state, action, reader.last_hand_card_diagnostics)
            if should_update_preview
            else None
        )
        timings["annotate_ms"] = round((time.perf_counter() - annotate_started) * 1000, 1)
        save_started = time.perf_counter()
        raw_path: Path | None = None
        annotated_path: Path | None = None
        if self._should_save_detection_images(config, frame_index):
            raw_path = self.logger.save_image(f"raw_{source}", image)
            if annotated is None:
                annotated = annotate_image(image, config, state, action, reader.last_hand_card_diagnostics)
            annotated_path = self.logger.save_image(f"annotated_{source}", annotated)
        timings["save_full_images_ms"] = round((time.perf_counter() - save_started) * 1000, 1)
        payload = {
            "source": source,
            "continuous": {
                "active": continuous_active,
                "frame_index": frame_index,
                "target_frame_ms": (
                    continuous_options["target_frame_ms"]
                    if continuous_active
                    else None
                ),
                "preview_every_n": continuous_options["preview_every_n"] if continuous_options else None,
                "last_processing_ms": self._continuous_last_processing_ms if continuous_active else None,
                "last_delay_ms": self._continuous_last_delay_ms if continuous_active else None,
            },
            "overrides": {
                "time": time_override,
                "phase": phase_override,
                "cost_enabled": self.cost_override_enabled.get(),
                "cost": self.cost_override_var.get().strip() if self.cost_override_enabled.get() else None,
            },
            "active_deck": self._deck_selection_payload(config),
            "screen_timer_seconds": reader.last_match_timer_seconds,
            "raw_timer_seconds": reader.last_raw_timer_seconds,
            "vision_diagnostics": {
                "layout_offset_x": state.layout_offset_x,
                "layout_offset_y": state.layout_offset_y,
                "local_calibration": reader.last_local_calibration,
                "layout_scores": reader.last_layout_scores,
                "read_state_timings": reader.last_read_state_timings,
                "timer_attempts": reader.last_timer_attempts,
                "timer_source": reader.last_timer_read_source,
                "timer_stabilizer": reader.last_timer_stabilizer,
                "cost_attempts": reader.last_cost_attempts,
                "hand_card_diagnostics": reader.last_hand_card_diagnostics,
                "battlefield_diagnostics": reader.last_battlefield_diagnostics,
            },
            "raw_image": str(raw_path.resolve()) if raw_path else None,
            "annotated_image": str(annotated_path.resolve()) if annotated_path else None,
            "state": _state_to_dict(state),
            "action": _display_action_to_dict(action, state.capture_origin, state.capture_scale),
        }
        live_play_card = self._maybe_execute_live_play_card(action, state, config, continuous_active)
        if live_play_card is not None:
            payload["live_play_card"] = live_play_card
        live_skill = self._maybe_execute_live_skill(action, state, config, reader, continuous_active)
        if live_skill is not None:
            payload["live_skill"] = live_skill
        if live_skill_pending is not None:
            payload["live_skill_pending"] = live_skill_pending
        training_payload = None
        training_started = time.perf_counter()
        if not continuous_active or (continuous_options and continuous_options["allow_inline_training"]):
            training_payload = self._save_hand_training_frame(image, config, state, payload)
        timings["save_training_frame_ms"] = round((time.perf_counter() - training_started) * 1000, 1)
        if training_payload is not None:
            payload["hand_training"] = training_payload
        timings["total_ms"] = round(
            float(timings.get("capture_ms", 0.0)) + (time.perf_counter() - total_started) * 1000,
            1,
        )
        payload["timings"] = timings
        self.logger.event("detect_result", payload)
        return "detect", payload, annotated

    def _update_pending_live_skill(
        self,
        reader: ScreenReader,
        state: GameState,
        config: BotConfig,
        continuous_active: bool,
    ) -> dict[str, Any] | None:
        pending = self._pending_live_skill
        if pending is None:
            return None
        if not continuous_active or not self.live_skill_var.get():
            self._pending_live_skill = None
            return {"status": "cleared", "reason": "live_skill_disabled", "skill_id": pending.get("skill_id")}

        skill_id = pending.get("skill_id")
        started_at = float(pending.get("started_monotonic", 0.0))
        elapsed = max(0.0, time.monotonic() - started_at)
        skill_state = next((item for item in state.skills if item.skill.id == skill_id), None)
        diagnostics = dict(skill_state.diagnostics) if skill_state else {}
        reason = diagnostics.get("visual_ready_reason")
        dark_ratio = diagnostics.get("dark_ratio")
        confirmed = reason == "cooldown_dark_overlay" or (
            isinstance(dark_ratio, (int, float))
            and float(dark_ratio) > config.skill_ready_max_dark_ratio()
        )

        if confirmed:
            executed_at = pending.get("state_now_seconds")
            reader.mark_action_executed(
                "cast_skill",
                str(skill_id) if skill_id else None,
                float(executed_at) if isinstance(executed_at, (int, float)) else state.now_seconds,
            )
            self._pending_live_skill = None
            self._suppress_skill_in_state(state, str(skill_id), "live_skill_confirmed")
            result = {
                "status": "confirmed",
                "skill_id": skill_id,
                "elapsed_seconds": round(elapsed, 3),
                "visual_ready_reason": reason,
                "dark_ratio": dark_ratio,
            }
            self.logger.event("live_skill_confirmed", result)
            return result

        timeout = max(0.0, float(config.screen.get("live_skill_confirm_timeout_seconds", 2.8)))
        if elapsed >= timeout:
            self._pending_live_skill = None
            result = {
                "status": "confirm_timeout",
                "skill_id": skill_id,
                "elapsed_seconds": round(elapsed, 3),
                "visual_ready_reason": reason,
                "dark_ratio": dark_ratio,
            }
            self.logger.event("live_skill_confirm_timeout", result)
            return result

        self._suppress_all_skills_in_state(state, f"waiting_confirm:{skill_id}")
        return {
            "status": "waiting_confirm",
            "skill_id": skill_id,
            "elapsed_seconds": round(elapsed, 3),
            "visual_ready_reason": reason,
            "dark_ratio": dark_ratio,
        }

    @staticmethod
    def _suppress_skill_in_state(state: GameState, skill_id: str, reason: str) -> None:
        updated = []
        for item in state.skills:
            if item.skill.id == skill_id:
                diagnostics = dict(item.diagnostics)
                diagnostics["live_skill_suppressed"] = reason
                updated.append(replace(item, ready=False, diagnostics=diagnostics))
            else:
                updated.append(item)
        state.skills = updated

    @staticmethod
    def _suppress_all_skills_in_state(state: GameState, reason: str) -> None:
        updated = []
        for item in state.skills:
            diagnostics = dict(item.diagnostics)
            diagnostics["live_skill_suppressed"] = reason
            updated.append(replace(item, ready=False, diagnostics=diagnostics))
        state.skills = updated

    def _maybe_execute_live_play_card(
        self,
        action: Action,
        state: GameState,
        config: BotConfig,
        continuous_active: bool,
    ) -> dict[str, Any] | None:
        if not continuous_active or not self.live_play_card_var.get():
            return None
        if action.type != ActionType.PLAY_CARD:
            return None
        if action.click is None:
            return {"enabled": True, "executed": False, "reason": "missing_click", "card_id": action.card_id}

        min_interval = max(0.0, float(config.screen.get("live_play_card_min_interval_seconds", 0.9)))
        signature = (action.card_id, action.click)
        now = time.monotonic()
        if (
            self._last_live_play_card_signature == signature
            and now - self._last_live_play_card_at < min_interval
        ):
            return {
                "enabled": True,
                "executed": False,
                "reason": "debounced",
                "card_id": action.card_id,
                "window_click": action.click,
            }

        screen_click = _offset_point(action.click, state.capture_origin, state.capture_scale)
        if screen_click is None:
            return {"enabled": True, "executed": False, "reason": "missing_screen_click", "card_id": action.card_id}

        try:
            import pyautogui

            pyautogui.FAILSAFE = True
            pyautogui.click(screen_click[0], screen_click[1])
        except Exception as exc:
            self.logger.event(
                "live_play_card_error",
                {
                    "card_id": action.card_id,
                    "window_click": action.click,
                    "screen_click": screen_click,
                    "error": str(exc),
                },
            )
            return {
                "enabled": True,
                "executed": False,
                "reason": "click_error",
                "card_id": action.card_id,
                "window_click": action.click,
                "screen_click": screen_click,
                "error": str(exc),
            }

        self._last_live_play_card_at = now
        self._last_live_play_card_signature = signature
        result = {
            "enabled": True,
            "executed": True,
            "card_id": action.card_id,
            "window_click": action.click,
            "screen_click": screen_click,
        }
        self.logger.event("live_play_card_executed", result)
        return result

    def _maybe_execute_live_skill(
        self,
        action: Action,
        state: GameState,
        config: BotConfig,
        reader: ScreenReader,
        continuous_active: bool,
    ) -> dict[str, Any] | None:
        if not continuous_active or not self.live_skill_var.get():
            return None
        if action.type != ActionType.CAST_SKILL:
            return None
        if action.click is None:
            return {"enabled": True, "executed": False, "reason": "missing_click", "skill_id": action.skill_id}

        min_interval = max(0.0, float(config.screen.get("live_skill_min_interval_seconds", 1.0)))
        signature = (action.skill_id, action.click, action.target_click)
        now = time.monotonic()
        if now - self._last_live_skill_at < min_interval:
            return {
                "enabled": True,
                "executed": False,
                "reason": "debounced",
                "skill_id": action.skill_id,
                "window_click": action.click,
                "window_target_click": action.target_click,
            }

        target_confirmation: dict[str, Any] | None = None
        if bool(config.screen.get("live_skill_refresh_target_before_click", False)):
            refresh = self._refresh_live_skill_target_before_click(action, state, config, reader)
            target_confirmation = refresh.get("target_confirmation")
            if not refresh.get("ok"):
                result = {
                    "enabled": True,
                    "executed": False,
                    "reason": refresh.get("reason", "target_refresh_failed"),
                    "skill_id": action.skill_id,
                    "window_click": action.click,
                    "window_target_click": action.target_click,
                    "target_confirmation": target_confirmation,
                    "error": refresh.get("error"),
                }
                event_name = (
                    "live_skill_target_refresh_error"
                    if result["reason"] == "target_refresh_error"
                    else "live_skill_target_confirmation_missing"
                )
                self.logger.event(event_name, result)
                return result
            action = refresh.get("action", action)
            state = refresh.get("state", state)
            if refresh.get("fallback"):
                self.logger.event(
                    "live_skill_target_refresh_fallback",
                    {
                        "skill_id": action.skill_id,
                        "reason": refresh.get("reason"),
                        "window_click": action.click,
                        "window_target_click": action.target_click,
                        "target_confirmation": target_confirmation,
                        "error": refresh.get("error"),
                    },
                )
        else:
            target_confirmation = _live_skill_target_confirmation(config, action.skill_id, state)

        screen_pre_clicks = [
            point
            for point in (_offset_point(click, state.capture_origin, state.capture_scale) for click in action.pre_clicks)
            if point is not None
        ]
        screen_click = _offset_point(action.click, state.capture_origin, state.capture_scale)
        screen_target_click = _offset_point(action.target_click, state.capture_origin, state.capture_scale)
        if screen_click is None:
            return {"enabled": True, "executed": False, "reason": "missing_screen_click", "skill_id": action.skill_id}

        click_delay = max(
            0.0,
            float(config.screen.get("live_skill_click_delay_seconds", config.global_click_delay_seconds)),
        )
        try:
            import pyautogui

            pyautogui.FAILSAFE = True
            for click in screen_pre_clicks:
                pyautogui.click(click[0], click[1])
                time.sleep(click_delay)
            pyautogui.click(screen_click[0], screen_click[1])
            time.sleep(click_delay)
            if screen_target_click:
                pyautogui.click(screen_target_click[0], screen_target_click[1])
                time.sleep(click_delay)
        except Exception as exc:
            self.logger.event(
                "live_skill_error",
                {
                    "skill_id": action.skill_id,
                    "window_pre_clicks": list(action.pre_clicks),
                    "window_click": action.click,
                    "window_target_click": action.target_click,
                    "screen_pre_clicks": screen_pre_clicks,
                    "screen_click": screen_click,
                    "screen_target_click": screen_target_click,
                    "error": str(exc),
                },
            )
            return {
                "enabled": True,
                "executed": False,
                "reason": "click_error",
                "skill_id": action.skill_id,
                "window_click": action.click,
                "window_target_click": action.target_click,
                "screen_click": screen_click,
                "screen_target_click": screen_target_click,
                "error": str(exc),
            }

        self._last_live_skill_at = now
        self._last_live_skill_signature = signature
        self._pending_live_skill = {
            "skill_id": action.skill_id,
            "started_monotonic": now,
            "state_now_seconds": state.now_seconds,
            "window_click": action.click,
            "window_target_click": action.target_click,
            "screen_click": screen_click,
            "screen_target_click": screen_target_click,
            "target_confirmation": target_confirmation,
        }
        result = {
            "enabled": True,
            "executed": True,
            "pending_confirmation": True,
            "skill_id": action.skill_id,
            "window_pre_clicks": list(action.pre_clicks),
            "window_click": action.click,
            "window_target_click": action.target_click,
            "screen_pre_clicks": screen_pre_clicks,
            "screen_click": screen_click,
            "screen_target_click": screen_target_click,
            "target_confirmation": target_confirmation,
        }
        self.logger.event("live_skill_executed", result)
        return result

    def _refresh_live_skill_target_before_click(
        self,
        action: Action,
        state: GameState,
        config: BotConfig,
        reader: ScreenReader,
    ) -> dict[str, Any]:
        initial_confirmation = _live_skill_target_confirmation(config, action.skill_id, state)
        if not initial_confirmation.get("required"):
            return {
                "ok": True,
                "action": action,
                "state": state,
                "target_confirmation": initial_confirmation,
            }
        window = self._continuous_window
        if window is None:
            return self._live_skill_target_refresh_fallback(
                action,
                state,
                "target_refresh_missing_window",
                initial_confirmation,
            )

        try:
            window = get_window(window.hwnd)
            self._continuous_window = window
            capture_started = time.perf_counter()
            image, origin, monitor = capture_window(window, config)
            capture_elapsed_ms = round((time.perf_counter() - capture_started) * 1000, 1)
            image, scale = normalize_capture_image(image, config)
            read_started = time.perf_counter()
            fresh_state = reader.read_state_from_image(
                image,
                now_seconds=state.now_seconds,
                phase_override=Phase.BATTLE,
                capture_origin=origin,
                capture_scale=scale,
            )
            self._apply_cost_override(fresh_state)
            read_elapsed_ms = round((time.perf_counter() - read_started) * 1000, 1)
        except Exception as exc:
            return self._live_skill_target_refresh_fallback(
                action,
                state,
                "target_refresh_error",
                initial_confirmation,
                error=str(exc),
            )

        fresh_confirmation = _live_skill_target_confirmation(config, action.skill_id, fresh_state)
        fresh_confirmation.update(
            {
                "capture_origin": origin,
                "capture_scale": scale,
                "capture_monitor": monitor,
                "timings": {
                    "capture_ms": capture_elapsed_ms,
                    "read_state_ms": read_elapsed_ms,
                },
                "previous_target_click": action.target_click,
            }
        )
        if not fresh_confirmation.get("confirmed"):
            return self._live_skill_target_refresh_fallback(
                action,
                fresh_state,
                "target_confirmation_missing",
                fresh_confirmation,
            )

        refreshed_action = replace(action, target_click=fresh_confirmation.get("target_click"))
        self.logger.event(
            "live_skill_target_confirmed",
            {
                "skill_id": action.skill_id,
                "target_confirmation": fresh_confirmation,
            },
        )
        return {
            "ok": True,
            "action": refreshed_action,
            "state": fresh_state,
            "target_confirmation": fresh_confirmation,
        }

    def _live_skill_target_refresh_fallback(
        self,
        action: Action,
        state: GameState,
        reason: str,
        target_confirmation: dict[str, Any],
        error: str | None = None,
    ) -> dict[str, Any]:
        target_confirmation = dict(target_confirmation)
        target_confirmation.update(
            {
                "confirmed": False,
                "execution": "target_confirmation_unverified",
                "fallback_reason": reason,
                "fallback_target_click": action.target_click,
                "previous_target_click": target_confirmation.get("previous_target_click", action.target_click),
            }
        )
        if error is not None:
            target_confirmation["error"] = error

        if action.target_click is None:
            return {
                "ok": False,
                "reason": reason,
                "target_confirmation": target_confirmation,
                "error": error,
            }

        return {
            "ok": True,
            "fallback": True,
            "reason": "target_refresh_fallback",
            "action": action,
            "state": state,
            "target_confirmation": target_confirmation,
            "error": error,
        }

    def _should_update_preview(self, config: BotConfig, frame_index: int | None) -> bool:
        if not self.continuous_window_var.get():
            return True
        if frame_index is None:
            return True
        every_n = int(self._continuous_screen_options(config)["preview_every_n"])
        return frame_index <= 1 or frame_index % every_n == 0

    def _save_hand_training_frame(
        self,
        image: Image.Image,
        config: BotConfig,
        state: GameState,
        detect_payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        with self._hand_training_lock:
            if (
                not self._hand_training_active
                or self._hand_training_external
                or self._hand_training_dir is None
                or self._hand_training_manifest_path is None
            ):
                return None
            self._hand_training_count += 1
            frame_index = self._hand_training_count
            training_dir = self._hand_training_dir
            manifest_path = self._hand_training_manifest_path

        stem = f"{frame_index:05d}"
        raw_training_path = training_dir / "raw" / f"{stem}_raw.png"
        image.save(raw_training_path)
        recognized_by_slot = {
            item.slot.name: {
                "card_id": item.card.id,
                "name": item.card.name,
                "confidence": round(item.confidence, 4),
            }
            for item in state.visible_cards
        }
        slots: list[dict[str, Any]] = []
        reader_for_rects = ScreenReader(config)
        hand_slots = _hand_slots_for_state(config, state)
        for slot in hand_slots:
            slot_path = training_dir / "slots" / f"{stem}_{_safe_file_stem(slot.name)}.png"
            title_path = training_dir / "titles" / f"{stem}_{_safe_file_stem(slot.name)}_title.png"
            crop_image(image, slot.rect).save(slot_path)
            title_rect = card_title_rect_for_slot(config, slot.rect)
            crop_image(image, title_rect).save(title_path)
            slots.append(
                {
                    "slot": slot.name,
                    "slot_rect": slot.rect,
                    "title_rect": title_rect,
                    "playable_guess": slot_looks_playable(config, image, slot.rect),
                    "recognized": recognized_by_slot.get(slot.name),
                    "slot_image": str(slot_path.resolve()),
                    "title_image": str(title_path.resolve()),
                }
            )

        timer_paths: list[dict[str, Any]] = []
        for index, rect in enumerate(reader_for_rects._timer_rects_for_offset(0)):
            path = training_dir / "timer" / f"{stem}_timer_{index}.png"
            crop_image(image, rect).save(path)
            timer_paths.append({"rect": rect, "image": str(path.resolve())})

        command_paths: list[dict[str, Any]] = []
        for name, rect in reader_for_rects._cost_rects_for_offset(0):
            path = training_dir / "command" / f"{stem}_command_{name}.png"
            crop_image(image, rect).save(path)
            command_paths.append({"name": name, "rect": rect, "image": str(path.resolve())})

        record = {
            "time": _now_iso(),
            "frame_index": frame_index,
            "session_frame_index": detect_payload.get("continuous", {}).get("frame_index"),
            "source": detect_payload.get("source"),
            "raw_image": detect_payload.get("raw_image"),
            "annotated_image": detect_payload.get("annotated_image"),
            "screen_timer_seconds": detect_payload.get("screen_timer_seconds"),
            "raw_timer_seconds": detect_payload.get("raw_timer_seconds"),
            "training_raw_image": str(raw_training_path.resolve()),
            "vision_diagnostics": detect_payload.get("vision_diagnostics"),
            "state": detect_payload.get("state"),
            "action": detect_payload.get("action"),
            "slots": slots,
            "timer": timer_paths,
            "command": command_paths,
            "label_fields": {
                "true_cards_by_slot": {},
                "true_timer_seconds": None,
                "true_command_value": None,
            },
        }
        text = json.dumps(record, ensure_ascii=False, default=str)
        with self._hand_training_lock:
            with manifest_path.open("a", encoding="utf-8") as handle:
                handle.write(text + "\n")
        self.logger.event(
            "hand_training_frame_saved",
            {
                "frame_index": frame_index,
                "dir": str(training_dir.resolve()),
                "slots": [
                    {
                        "slot": item["slot"],
                        "playable_guess": item["playable_guess"],
                        "recognized": item["recognized"],
                    }
                    for item in slots
                ],
            },
        )
        return {
            "active": True,
            "dir": str(training_dir.resolve()),
            "manifest": str(manifest_path.resolve()),
            "frame_index": frame_index,
        }

    def _poll_queue(self) -> None:
        try:
            while True:
                kind, payload = self._queue.get_nowait()
                self._worker_busy = False
                if kind == "error":
                    self._show_error(payload)
                else:
                    result = payload["result"]
                    self._show_result(result)
                    self._schedule_next_continuous_after_result(result)
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _schedule_next_continuous_after_result(self, result: tuple[str, dict[str, Any], Image.Image | None]) -> None:
        if not self.continuous_window_var.get():
            return
        result_type, payload, _image = result
        if result_type != "detect":
            return

        timings = payload.get("timings", {})
        processing_ms = timings.get("total_ms")
        try:
            processing_ms = float(processing_ms)
        except (TypeError, ValueError):
            processing_ms = None

        self._continuous_last_processing_ms = processing_ms
        delay_ms = self._adaptive_continuous_delay_ms(processing_ms)
        self._continuous_last_delay_ms = delay_ms
        self.logger.event(
            "continuous_window_next",
            {
                "frame_index": payload.get("continuous", {}).get("frame_index"),
                "processing_ms": processing_ms,
                "next_delay_ms": delay_ms,
            },
        )
        self._schedule_continuous_window(delay_ms)

    def _show_error(self, text: str) -> None:
        if isinstance(text, dict):
            payload = text
            display_text = str(text.get("traceback", text))
        else:
            payload = {"traceback": text}
            display_text = text
        self.logger.event("worker_error", payload)
        self._stop_continuous_window("error")
        self.status_var.set("Error")
        self.output_text.delete("1.0", "end")
        self.output_text.insert("1.0", display_text)
        lines = display_text.splitlines()
        messagebox.showerror("运行错误", lines[-1] if lines else display_text)

    def _show_result(self, result: tuple[str, dict[str, Any], Image.Image | None]) -> None:
        result_type, payload, image = result
        update_preview = self._should_update_result_preview(payload)
        if update_preview:
            self.output_text.delete("1.0", "end")
            self.output_text.insert("1.0", json.dumps(payload, ensure_ascii=False, indent=2))

        if result_type == "assets":
            missing = payload.get("missing", 0)
            self.logger.event("assets_result", payload)
            self.summary_var.set(f"素材校验: missing={missing}")
            self.status_var.set("素材校验完成")
            return

        if result_type == "calibration":
            self.logger.event("auto_calibration_result", payload)
            calibration = payload.get("calibration") if isinstance(payload.get("calibration"), dict) else {}
            offset_x = calibration.get("offset_x", 0)
            offset_y = calibration.get("offset_y", 0)
            score = calibration.get("score", 0.0)
            min_score = calibration.get("min_score", 0.0)
            if payload.get("ok"):
                self.calibration_value_var.set(f"完成 {offset_x},{offset_y}")
                self.summary_var.set(f"自动校准完成: offset=({offset_x},{offset_y}) score={score:.2f}")
                self.status_var.set("自动校准完成")
            else:
                stability = payload.get("stability") if isinstance(payload.get("stability"), dict) else {}
                reason = str(stability.get("reason") or "score_below_threshold")
                stable_count = stability.get("stable_count", 0)
                required_count = stability.get("required_count", 0)
                self.calibration_value_var.set("失败")
                if reason == "insufficient_stable_samples":
                    self.summary_var.set(
                        f"自动校准失败: 样本不稳定 {stable_count}/{required_count}, score={score:.2f}"
                    )
                else:
                    self.summary_var.set(f"自动校准失败: score={score:.2f} < {min_score:.2f}")
                self.status_var.set("自动校准失败")
            if image and update_preview:
                self._last_annotated = image
                self._display_image(image)
            return

        action = payload["action"]
        live_play_card = payload.get("live_play_card") if isinstance(payload.get("live_play_card"), dict) else None
        live_skill = payload.get("live_skill") if isinstance(payload.get("live_skill"), dict) else None
        self._update_dashboard_cards(payload)
        self._update_last_action(action, live_play_card, live_skill)
        if image and update_preview:
            self._last_annotated = image
            self._display_image(image)
        if live_play_card and live_play_card.get("executed"):
            self.status_var.set(f"已点击手牌 {live_play_card.get('card_id')}")
        elif live_skill and live_skill.get("executed"):
            self.status_var.set(f"已释放技能 {live_skill.get('skill_id')}")
        else:
            self.status_var.set("识别完成")

    def _update_dashboard_cards(self, payload: dict[str, Any]) -> None:
        state = payload.get("state", {})
        self.time_value_var.set(str(state.get("time", "--")))
        self.cost_value_var.set(str(state.get("cost", "--")))
        self.phase_value_var.set(f"阶段: {state.get('phase', '--')}")
        calibration = payload.get("vision_diagnostics", {}).get("local_calibration")
        if isinstance(calibration, dict):
            self.calibration_value_var.set(
                f"完成 {calibration.get('offset_x', 0)},{calibration.get('offset_y', 0)}"
            )
        elif self.calibration_value_var.get() != "进行中":
            self.calibration_value_var.set("未完成")

        visible_by_slot = {str(item.get("slot")): item for item in state.get("visible_cards", [])}
        playable = state.get("hand_slot_playable", {})
        diagnostics = (
            payload.get("vision_diagnostics", {})
            .get("hand_card_diagnostics", {})
            .get("slots", {})
        )
        for index, vars_for_card in enumerate(self.hand_card_vars, start=1):
            slot_name = f"hand_{index}"
            item = visible_by_slot.get(slot_name)
            slot_diag = diagnostics.get(slot_name, {})
            slot_playable = playable.get(slot_name)
            vars_for_card["title"].set(f"手牌 {index}")
            if item:
                vars_for_card["name"].set(str(item.get("name") or item.get("card_id") or "未知卡牌"))
                vars_for_card["confidence"].set(_format_confidence(item.get("confidence")))
                if slot_playable is False:
                    vars_for_card["state"].set("灰牌")
                    self.hand_status_labels[index - 1].configure(style="CardStateMuted.TLabel")
                else:
                    vars_for_card["state"].set("可用")
                    self.hand_status_labels[index - 1].configure(style="CardStateReady.TLabel")
                continue

            vars_for_card["name"].set("未识别")
            vars_for_card["confidence"].set(_format_confidence(slot_diag.get("confidence")))
            if slot_diag.get("playable") is False or slot_playable is False:
                vars_for_card["state"].set("灰牌")
                self.hand_status_labels[index - 1].configure(style="CardStateMuted.TLabel")
            else:
                vars_for_card["state"].set("空位/未知")
                self.hand_status_labels[index - 1].configure(style="CardStateWarn.TLabel")

        seen_skills: set[str] = set()
        for skill in state.get("skills", []):
            skill_id = str(skill.get("skill_id"))
            vars_for_skill = self.skill_card_vars.get(skill_id)
            if vars_for_skill is None:
                continue
            seen_skills.add(skill_id)
            vars_for_skill["title"].set(skill_id)
            vars_for_skill["name"].set(str(skill.get("name") or skill_id))
            vars_for_skill["confidence"].set(_format_confidence(skill.get("confidence")))
            status_label = self.skill_status_labels[skill_id]
            if bool(skill.get("ready")):
                vars_for_skill["state"].set("就绪")
                status_label.configure(style="CardStateReady.TLabel")
                continue
            diagnostics = skill.get("diagnostics") if isinstance(skill.get("diagnostics"), dict) else {}
            if diagnostics.get("live_skill_suppressed"):
                vars_for_skill["state"].set("确认中")
                status_label.configure(style="CardStateWarn.TLabel")
            elif skill.get("confidence", 0) == 0 and not diagnostics:
                vars_for_skill["state"].set("未启用")
                status_label.configure(style="CardStateMuted.TLabel")
            else:
                vars_for_skill["state"].set("冷却")
                status_label.configure(style="CardStateMuted.TLabel")

        for skill_id, vars_for_skill in self.skill_card_vars.items():
            if skill_id in seen_skills:
                continue
            vars_for_skill["state"].set("未识别")
            vars_for_skill["confidence"].set("--")
            self.skill_status_labels[skill_id].configure(style="CardStateDanger.TLabel")

    def _update_last_action(
        self,
        action: dict[str, Any],
        live_play_card: dict[str, Any] | None,
        live_skill: dict[str, Any] | None,
    ) -> None:
        if live_play_card and live_play_card.get("executed"):
            self.last_action_var.set(f"手牌执行: {live_play_card.get('card_id')}")
        elif live_skill and live_skill.get("executed"):
            self.last_action_var.set(f"技能执行: {live_skill.get('skill_id')}")
        elif action.get("action") == "wait":
            self.last_action_var.set("等待下一次可执行动作")
        elif action.get("card_id"):
            self.last_action_var.set(f"准备手牌: {action.get('card_id')}")
        elif action.get("skill_id"):
            self.last_action_var.set(f"准备技能: {action.get('skill_id')}")
        else:
            self.last_action_var.set("识别运行中")

    def _should_update_result_preview(self, payload: dict[str, Any]) -> bool:
        continuous = payload.get("continuous", {})
        if not continuous.get("active"):
            return True
        try:
            frame_number = int(continuous.get("frame_index"))
        except (TypeError, ValueError):
            return True
        try:
            every_n = int(continuous.get("preview_every_n") or self._continuous_screen_options()["preview_every_n"])
        except Exception:
            every_n = 1
        return frame_number <= 1 or frame_number % max(1, every_n) == 0

    def _display_image(self, image: Image.Image) -> None:
        max_width = max(500, self.preview_label.winfo_width() - 20)
        max_height = max(360, self.preview_label.winfo_height() - 20)
        display = image.copy()
        display.thumbnail((max_width, max_height), Image.Resampling.LANCZOS)
        self._photo = ImageTk.PhotoImage(display)
        self.preview_label.configure(image=self._photo)


def launch_gui(config: str | Path | None = None) -> None:
    config_path = Path(config).resolve() if config else None
    app = LagrangeTestGui(config_path=config_path)
    app.mainloop()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Launch the Lagrange bot GUI test panel.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="Config JSON path.")
    args = parser.parse_args(argv)
    launch_gui(args.config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
