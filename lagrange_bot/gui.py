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
from .models import Action, ActionType, GameState, Phase
from .vision import (
    ScreenReader,
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
    if not state.layout_offset_y:
        return config.hand_slots
    return [
        slot.__class__(
            name=slot.name,
            rect=_offset_rect(slot.rect, 0, state.layout_offset_y),
            click=(slot.click[0], slot.click[1] + state.layout_offset_y),
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

    if state.layout_offset_y:
        for slot in config.hand_slots:
            shifted = _offset_rect(slot.rect, 0, state.layout_offset_y)
            draw.rectangle(_rect_xyxy(shifted), outline="#22c55e", width=2)
            draw.text((shifted[0] + 4, shifted[1] + 4), f"{slot.name}+{state.layout_offset_y}", fill="#22c55e")

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


def capture_window(window: WindowInfo, config: BotConfig) -> tuple[Image.Image, tuple[int, int], dict[str, int]]:
    return capture_window_client(window, config)


class LagrangeTestGui(tk.Tk):
    def __init__(self, config_path: Path | None = None):
        super().__init__()
        self.title("Lagrange Bot Test Panel")
        self.geometry("1220x780")
        self.minsize(980, 620)

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
        self.live_play_card_var = tk.BooleanVar(value=False)
        self.live_skill_var = tk.BooleanVar(value=False)
        self.hand_training_var = tk.BooleanVar(value=False)
        self.battle_training_var = tk.BooleanVar(value=False)

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
        self.columnconfigure(0, weight=3)
        self.columnconfigure(1, weight=2)
        self.rowconfigure(1, weight=1)

        controls = ttk.Frame(self, padding=(10, 8))
        controls.grid(row=0, column=0, columnspan=2, sticky="ew")
        controls.columnconfigure(1, weight=1)
        controls.columnconfigure(4, weight=1)

        ttk.Label(controls, text="配置").grid(row=0, column=0, sticky="w")
        ttk.Entry(controls, textvariable=self.config_path_var).grid(
            row=0, column=1, columnspan=3, sticky="ew", padx=(6, 6)
        )
        ttk.Button(controls, text="选择", command=self._browse_config).grid(row=0, column=4, sticky="w")
        ttk.Button(controls, text="校验素材", command=self.validate_assets_clicked).grid(
            row=0, column=5, padx=(8, 0)
        )

        ttk.Label(controls, text="截图").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(controls, textvariable=self.image_path_var).grid(
            row=1, column=1, columnspan=3, sticky="ew", padx=(6, 6), pady=(8, 0)
        )
        ttk.Button(controls, text="选择", command=self._browse_image).grid(row=1, column=4, sticky="w", pady=(8, 0))
        ttk.Button(controls, text="识别截图", command=self.detect_image_clicked).grid(
            row=1, column=5, padx=(8, 0), pady=(8, 0)
        )
        ttk.Button(controls, text="截屏识别", command=self.capture_clicked).grid(
            row=1, column=6, padx=(8, 0), pady=(8, 0)
        )

        ttk.Label(controls, text="窗口").grid(row=2, column=0, sticky="w", pady=(8, 0))
        self.window_combo = ttk.Combobox(controls, textvariable=self.window_var, state="readonly")
        self.window_combo.grid(row=2, column=1, columnspan=3, sticky="ew", padx=(6, 6), pady=(8, 0))
        ttk.Button(controls, text="刷新窗口", command=self.refresh_windows_clicked).grid(
            row=2, column=4, sticky="w", pady=(8, 0)
        )
        ttk.Button(controls, text="窗口识别", command=self.detect_window_clicked).grid(
            row=2, column=5, padx=(8, 0), pady=(8, 0)
        )
        self.continuous_button = ttk.Button(
            controls,
            text="开始连续识别",
            command=self.toggle_continuous_window_clicked,
        )
        self.continuous_button.grid(row=2, column=6, padx=(8, 0), pady=(8, 0))
        self.hand_training_button = ttk.Button(
            controls,
            text="开始手牌采集",
            command=self.toggle_hand_training_clicked,
        )
        self.hand_training_button.grid(row=2, column=7, padx=(8, 0), pady=(8, 0))
        self.battle_training_button = ttk.Button(
            controls,
            text="开始战斗采集",
            command=self.toggle_battle_training_clicked,
        )
        self.battle_training_button.grid(row=2, column=8, padx=(8, 0), pady=(8, 0))

        options = ttk.Frame(controls)
        options.grid(row=3, column=1, columnspan=7, sticky="ew", pady=(8, 0))
        ttk.Label(options, text="时间秒").pack(side="left")
        ttk.Entry(options, textvariable=self.time_var, width=8).pack(side="left", padx=(6, 14))
        ttk.Label(options, text="阶段").pack(side="left")
        ttk.Combobox(
            options,
            textvariable=self.phase_override_var,
            values=["auto", "placement", "extra_place", "battle"],
            state="readonly",
            width=11,
        ).pack(side="left", padx=(6, 14))
        ttk.Checkbutton(options, text="覆盖费用", variable=self.cost_override_enabled).pack(side="left")
        ttk.Entry(options, textvariable=self.cost_override_var, width=8).pack(side="left", padx=(6, 14))
        ttk.Checkbutton(options, text="执行手牌点击", variable=self.live_play_card_var).pack(side="left", padx=(0, 14))
        ttk.Checkbutton(options, text="执行技能点击", variable=self.live_skill_var).pack(side="left", padx=(0, 14))
        ttk.Label(
            options,
            text="提示: 75 秒后测试布阵, 140 秒测试战斗; 覆盖费用只影响决策预览; 勾选执行项才会真实点击。",
        ).pack(side="left")

        preview_frame = ttk.Frame(self, padding=(10, 0, 5, 6))
        preview_frame.grid(row=1, column=0, sticky="nsew")
        preview_frame.rowconfigure(0, weight=1)
        preview_frame.columnconfigure(0, weight=1)

        self.preview_label = ttk.Label(preview_frame, anchor="center", background="#111827")
        self.preview_label.grid(row=0, column=0, sticky="nsew")

        output_frame = ttk.Frame(self, padding=(5, 0, 10, 6))
        output_frame.grid(row=1, column=1, sticky="nsew")
        output_frame.rowconfigure(1, weight=1)
        output_frame.columnconfigure(0, weight=1)

        summary = ttk.LabelFrame(output_frame, text="动作建议", padding=8)
        summary.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        summary.columnconfigure(0, weight=1)
        self.summary_var = tk.StringVar(value="还没有识别结果")
        ttk.Label(summary, textvariable=self.summary_var, justify="left").grid(row=0, column=0, sticky="w")

        details = ttk.LabelFrame(output_frame, text="状态 JSON", padding=4)
        details.grid(row=1, column=0, sticky="nsew")
        details.rowconfigure(0, weight=1)
        details.columnconfigure(0, weight=1)
        self.output_text = tk.Text(details, wrap="none", height=10)
        self.output_text.grid(row=0, column=0, sticky="nsew")
        yscroll = ttk.Scrollbar(details, orient="vertical", command=self.output_text.yview)
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll = ttk.Scrollbar(details, orient="horizontal", command=self.output_text.xview)
        xscroll.grid(row=1, column=0, sticky="ew")
        self.output_text.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)

        status = ttk.Label(self, textvariable=self.status_var, relief="sunken", anchor="w", padding=(8, 3))
        status.grid(row=2, column=0, columnspan=2, sticky="ew")
        self.refresh_windows_clicked()
        self.status_var.set(f"Ready | log: {self.logger.dir}")

    def _browse_config(self) -> None:
        path = filedialog.askopenfilename(
            title="选择配置",
            filetypes=[("JSON config", "*.json"), ("All files", "*.*")],
            initialdir=str(Path("configs").resolve()),
        )
        if path:
            self.config_path_var.set(path)
            self.logger.event("config_selected", {"path": path})

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
                "time_override": self.time_var.get(),
                "phase_override": self.phase_override_var.get(),
                "cost_override_enabled": self.cost_override_enabled.get(),
                "cost_override": self.cost_override_var.get(),
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

        try:
            window = self._selected_window()
        except Exception as exc:
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
        self.continuous_button.configure(text="停止连续识别")
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
        self._continuous_last_processing_ms = None
        self._continuous_last_delay_ms = 0
        self._last_continuous_capture_rect = None
        self._last_continuous_rect_change_monotonic = None
        self._opening_zoom_active = False
        self.continuous_button.configure(text="开始连续识别")
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
        config = self._load_config()
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
        config = self._load_config()
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
        reader = reader or ScreenReader(config)
        timings = dict(timings or {})
        total_started = time.perf_counter()
        continuous_active = self.continuous_window_var.get()
        frame_index = self._continuous_count if continuous_active else None
        continuous_options = self._continuous_screen_options(config) if continuous_active else None
        read_started = time.perf_counter()
        phase_override_value = self._phase_override_value()
        state = reader.read_state_from_image(
            image,
            now_seconds=self._parse_time_override(),
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
                "time": self.time_var.get().strip() or None,
                "phase": phase_override,
                "cost_enabled": self.cost_override_enabled.get(),
                "cost": self.cost_override_var.get().strip() if self.cost_override_enabled.get() else None,
            },
            "screen_timer_seconds": reader.last_match_timer_seconds,
            "raw_timer_seconds": reader.last_raw_timer_seconds,
            "vision_diagnostics": {
                "layout_offset_y": state.layout_offset_y,
                "layout_scores": reader.last_layout_scores,
                "read_state_timings": reader.last_read_state_timings,
                "timer_attempts": reader.last_timer_attempts,
                "timer_source": reader.last_timer_read_source,
                "timer_stabilizer": reader.last_timer_stabilizer,
                "cost_attempts": reader.last_cost_attempts,
                "hand_card_diagnostics": reader.last_hand_card_diagnostics,
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
            reader.mark_action_executed("cast_skill", str(skill_id) if skill_id else None)
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
        hand_slots = reader_for_rects._hand_slots_for_offset(state.layout_offset_y)
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
        for index, rect in enumerate(reader_for_rects._timer_rects_for_offset(state.layout_offset_y)):
            path = training_dir / "timer" / f"{stem}_timer_{index}.png"
            crop_image(image, rect).save(path)
            timer_paths.append({"rect": rect, "image": str(path.resolve())})

        command_paths: list[dict[str, Any]] = []
        for name, rect in reader_for_rects._cost_rects_for_offset(state.layout_offset_y):
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

        action = payload["action"]
        state = payload["state"]
        hand_summary = _format_hand_summary(payload)
        skill_summary = _format_skill_summary(payload)
        battlefield_summary = _format_battlefield_summary(payload)
        live_play_card = payload.get("live_play_card") if isinstance(payload.get("live_play_card"), dict) else None
        live_skill = payload.get("live_skill") if isinstance(payload.get("live_skill"), dict) else None
        live_summary = ""
        if live_play_card:
            if live_play_card.get("executed"):
                live_summary = f"手牌点击: {live_play_card.get('card_id')} @ {live_play_card.get('screen_click')}"
            else:
                live_summary = f"手牌点击: {live_play_card.get('reason')}"
        skill_live_summary = ""
        if live_skill:
            if live_skill.get("executed"):
                skill_live_summary = (
                    f"技能点击: {live_skill.get('skill_id')} @ "
                    f"{live_skill.get('screen_click')} -> {live_skill.get('screen_target_click')}"
                )
            else:
                skill_live_summary = f"技能点击: {live_skill.get('reason')}"
        self.summary_var.set(
            "\n".join(
                line for line in [
                    f"阶段: {state['phase']}    时间: {state['time']}s    费用: {state['cost']}    原点: {state['capture_origin']}",
                    f"动作: {action['action']}",
                    f"原因: {action['reason']}",
                    f"卡牌: {hand_summary}",
                    f"技能: {skill_summary}    本次: {action['skill_id']}",
                    f"战场目标: {battlefield_summary}",
                    f"窗口内: {action['pre_clicks']} / {action['click']} / {action['target_click']}",
                    f"屏幕上: {action['screen_pre_clicks']} / {action['screen_click']} / {action['screen_target_click']}",
                    live_summary,
                    skill_live_summary,
                ] if line
            )
        )
        if image and update_preview:
            self._last_annotated = image
            self._display_image(image)
        if live_play_card and live_play_card.get("executed"):
            self.status_var.set(f"已点击手牌 {live_play_card.get('card_id')} | log: {self.logger.dir}")
        elif live_skill and live_skill.get("executed"):
            self.status_var.set(f"已释放技能 {live_skill.get('skill_id')} | log: {self.logger.dir}")
        else:
            self.status_var.set(f"识别完成 | log: {self.logger.dir}")

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
