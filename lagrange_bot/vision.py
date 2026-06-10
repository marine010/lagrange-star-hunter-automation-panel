from __future__ import annotations

from dataclasses import replace
from functools import lru_cache
import json
import threading
import time
from pathlib import Path
from typing import Any

from .capture_backends import capture_monitor_for_rect, capture_region as _screen_capture_region, capture_window_client
from .config import BotConfig
from .models import BattlefieldTarget, GameState, MatchResult, Phase, SkillState, SlotConfig, VisibleCard


_TEMPLATE_IMAGE_CACHE_LOCK = threading.Lock()
_TEMPLATE_IMAGE_CACHE: dict[tuple[str, int, int, int], Any] = {}
_NO_VISIBLE_CARD = object()


class _FrameCache:
    def __init__(self, image: Any, frame_index: int):
        self.image = image
        self.image_id = id(image)
        self.frame_index = int(frame_index)
        self._rgb: Any | None = None
        self._gray: Any | None = None
        self._rgb_rois: dict[tuple[int, int, int, int], Any] = {}
        self._gray_rois: dict[tuple[int, int, int, int], Any] = {}
        self._bgr_rois: dict[tuple[int, int, int, int], Any] = {}
        self.playable: dict[tuple[int, int, int, int], bool] = {}
        self.layout_scores: dict[tuple[int, int, int, int], float] = {}
        self.fingerprints: dict[tuple[int, int, int, int], tuple[int, ...]] = {}

    def matches(self, image: Any) -> bool:
        return id(image) == self.image_id

    def rgb(self) -> Any:
        if self._rgb is None:
            import numpy as np

            self._rgb = np.asarray(self.image.convert("RGB"), dtype=np.uint8)
        return self._rgb

    def gray(self) -> Any:
        if self._gray is None:
            import numpy as np

            self._gray = np.asarray(self.image.convert("L"), dtype=np.uint8)
        return self._gray

    def rgb_roi(self, rect: tuple[int, int, int, int]) -> Any:
        rect = tuple(int(v) for v in rect)
        cached = self._rgb_rois.get(rect)
        if cached is not None:
            return cached
        roi = _array_roi(self.rgb(), rect)
        self._rgb_rois[rect] = roi
        return roi

    def gray_roi(self, rect: tuple[int, int, int, int]) -> Any:
        rect = tuple(int(v) for v in rect)
        cached = self._gray_rois.get(rect)
        if cached is not None:
            return cached
        roi = _array_roi(self.gray(), rect)
        self._gray_rois[rect] = roi
        return roi

    def bgr_roi(self, rect: tuple[int, int, int, int]) -> Any:
        rect = tuple(int(v) for v in rect)
        cached = self._bgr_rois.get(rect)
        if cached is not None:
            return cached
        import numpy as np

        rgb = self.rgb_roi(rect)
        if rgb.size == 0:
            bgr = np.empty((0, 0, 3), dtype=np.uint8)
        else:
            cv2 = _load_cv2()
            bgr = cv2.cvtColor(np.ascontiguousarray(rgb), cv2.COLOR_RGB2BGR)
        self._bgr_rois[rect] = bgr
        return bgr


def _load_cv2():
    try:
        import cv2
    except ImportError as exc:
        raise RuntimeError("opencv-python is required for image matching") from exc
    return cv2


def _load_pil_image():
    try:
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError("Pillow is required for image processing") from exc
    return Image


def _cached_cv2_imread(template_path: str | Path, flags: int) -> Any:
    path = Path(template_path)
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    key = (str(path.resolve()), int(flags), int(stat.st_mtime_ns), int(stat.st_size))
    with _TEMPLATE_IMAGE_CACHE_LOCK:
        cached = _TEMPLATE_IMAGE_CACHE.get(key)
        if cached is not None:
            return cached

    cv2 = _load_cv2()
    image = cv2.imread(str(path), flags)
    if image is not None:
        with _TEMPLATE_IMAGE_CACHE_LOCK:
            _TEMPLATE_IMAGE_CACHE[key] = image
    return image


@lru_cache(maxsize=256)
def _glob_template_paths(directory_text: str, pattern: str) -> tuple[str, ...]:
    return tuple(str(path) for path in Path(directory_text).glob(pattern))


class ScreenReader:
    def __init__(self, config: BotConfig):
        self.config = config
        self.start_monotonic = time.monotonic()
        self.last_skill_cast: dict[str, float] = {}
        self.last_capture_origin: tuple[int, int] = (0, 0)
        self.last_capture_scale: tuple[float, float] = (1.0, 1.0)
        self.last_match_timer_seconds: float | None = None
        self.last_raw_timer_seconds: float | None = None
        self.last_layout_offset_y: int = 0
        self.last_layout_scores: dict[int, float] = {}
        self.last_read_state_timings: dict[str, float] = {}
        self.last_timer_attempts: list[dict[str, Any]] = []
        self.last_cost_attempts: list[dict[str, Any]] = []
        self.last_hand_card_diagnostics: dict[str, Any] = {}
        self.last_battlefield_diagnostics: dict[str, Any] = {}
        self.last_timer_read_source: str | None = None
        self.last_timer_stabilizer: dict[str, Any] = {}
        self._card_title_templates: dict[str, list[str]] | None = None
        self._card_title_templates_key: tuple[Any, ...] | None = None
        self._card_templates: dict[str, str] | None = None
        self._card_templates_key: tuple[Any, ...] | None = None
        self._battlefield_search_rect_cache: dict[str, tuple[tuple[int, int, int, int], dict[str, Any]]] = {}
        self._slot_card_cache: dict[str, dict[str, Any]] = {}
        self._stable_timer_seconds: float | None = None
        self._stable_timer_monotonic: float | None = None
        self._last_timer_ocr_monotonic: float | None = None
        self._last_timer_ocr_seconds: float | None = None
        self._active_frame_cache: _FrameCache | None = None
        self._frame_cache_index = 0

    def capture(self) -> Any:
        rect = self.config.capture_rect()
        window_title = self.config.screen.get("window_title_contains")
        monitor: dict[str, int]
        if window_title:
            from .windowing import find_window

            window = find_window(str(window_title))
            image, origin, _monitor = capture_window_client(window, self.config)
            self.last_capture_origin = origin
            image, self.last_capture_scale = normalize_capture_image(image, self.config)
            return image
        elif rect is None:
            try:
                import mss
            except ImportError:
                from PIL import ImageGrab

                image = ImageGrab.grab()
                self.last_capture_origin = (0, 0)
                image, self.last_capture_scale = normalize_capture_image(image, self.config)
                return image.convert("RGB")

            with mss.mss() as sct:
                raw_monitor = sct.monitors[1]
                monitor = {
                    "left": int(raw_monitor["left"]),
                    "top": int(raw_monitor["top"]),
                    "width": int(raw_monitor["width"]),
                    "height": int(raw_monitor["height"]),
                }
            self.last_capture_origin = (int(monitor["left"]), int(monitor["top"]))
        else:
            left, top, width, height = rect
            monitor, origin = capture_monitor_for_rect(self.config, left, top, width, height)
            self.last_capture_origin = origin

        image = _screen_capture_region(monitor, self.config)

        image, self.last_capture_scale = normalize_capture_image(image, self.config)
        return image

    def read_state(self) -> GameState:
        image = self.capture()
        return self.read_state_from_image(
            image,
            capture_origin=self.last_capture_origin,
            capture_scale=self.last_capture_scale,
        )

    def read_state_from_image(
        self,
        image: Any,
        now_seconds: float | None = None,
        phase_override: Phase | None = None,
        capture_origin: tuple[int, int] = (0, 0),
        capture_scale: tuple[float, float] = (1.0, 1.0),
    ) -> GameState:
        total_started = time.perf_counter()
        timings: dict[str, float] = {}
        self.last_timer_attempts = []
        self.last_cost_attempts = []
        self.last_timer_read_source = None
        self.last_timer_stabilizer = {}
        self._frame_cache_index += 1
        previous_cache = self._active_frame_cache
        self._active_frame_cache = _FrameCache(image, self._frame_cache_index)

        try:
            step_started = time.perf_counter()
            layout_offset_y = self._select_layout_offset_y(image)
            timings["layout_ms"] = round((time.perf_counter() - step_started) * 1000, 1)

            step_started = time.perf_counter()
            timer_seconds = self._read_match_timer_seconds(image, layout_offset_y)
            timings["timer_ms"] = round((time.perf_counter() - step_started) * 1000, 1)
            self.last_raw_timer_seconds = timer_seconds

            step_started = time.perf_counter()
            timer_seconds = self._stabilize_match_timer(timer_seconds)
            timings["timer_stabilize_ms"] = round((time.perf_counter() - step_started) * 1000, 1)
            self.last_match_timer_seconds = timer_seconds
            if timer_seconds is not None:
                now_seconds = timer_seconds
            elif now_seconds is None:
                now_seconds = time.monotonic() - self.start_monotonic

            step_started = time.perf_counter()
            phase = phase_override or self._read_phase(now_seconds)
            timings["phase_ms"] = round((time.perf_counter() - step_started) * 1000, 1)

            step_started = time.perf_counter()
            cost = 0 if phase == Phase.BATTLE else self._read_cost(image, layout_offset_y)
            timings["cost_ms"] = round((time.perf_counter() - step_started) * 1000, 1)

            step_started = time.perf_counter()
            hand_slot_playable = (
                {}
                if phase == Phase.BATTLE
                else self._read_hand_slot_playable(image, layout_offset_y)
            )
            timings["hand_playable_ms"] = round((time.perf_counter() - step_started) * 1000, 1)

            step_started = time.perf_counter()
            visible_cards = (
                []
                if phase == Phase.BATTLE
                else self._read_cards(image, hand_slot_playable, layout_offset_y, current_cost=cost)
            )
            timings["cards_ms"] = round((time.perf_counter() - step_started) * 1000, 1)

            step_started = time.perf_counter()
            reserve_card_id = (
                self._read_reserve(image, layout_offset_y)
                if phase != Phase.BATTLE and bool(self.config.screen.get("read_reserve", False))
                else None
            )
            timings["reserve_ms"] = round((time.perf_counter() - step_started) * 1000, 1)

            step_started = time.perf_counter()
            skills = self._read_skills(image, now_seconds)
            timings["skills_ms"] = round((time.perf_counter() - step_started) * 1000, 1)

            step_started = time.perf_counter()
            battlefield_targets = self._read_battlefield_targets(image, phase)
            timings["battlefield_ms"] = round((time.perf_counter() - step_started) * 1000, 1)

            state = GameState(
                now_seconds=now_seconds,
                phase=phase,
                cost=cost,
                visible_cards=visible_cards,
                reserve_card_id=reserve_card_id,
                played_counts={},
                skills=skills,
                battlefield_targets=battlefield_targets,
                hand_slot_playable=hand_slot_playable,
                layout_offset_y=layout_offset_y,
                capture_origin=capture_origin,
                capture_scale=capture_scale,
            )
            timings["total_ms"] = round((time.perf_counter() - total_started) * 1000, 1)
            self.last_read_state_timings = timings
            return state
        finally:
            self._active_frame_cache = previous_cache

    def _stabilize_match_timer(self, timer_seconds: float | None) -> float | None:
        current_monotonic = time.monotonic()
        if timer_seconds is not None:
            read_source = self.last_timer_read_source or "ocr"
            if self._stable_timer_seconds is None:
                self._stable_timer_seconds = timer_seconds
                self._stable_timer_monotonic = current_monotonic
                if read_source == "ocr":
                    self._last_timer_ocr_seconds = timer_seconds
                self.last_timer_stabilizer = {
                    "decision": "initial",
                    "source": read_source,
                    "seconds": round(timer_seconds, 3),
                }
                return timer_seconds

            if read_source == "extrapolated":
                self._stable_timer_seconds = timer_seconds
                self._stable_timer_monotonic = current_monotonic
                self.last_timer_stabilizer = {
                    "decision": "extrapolated",
                    "seconds": round(timer_seconds, 3),
                }
                return timer_seconds

            elapsed = 0.0
            if self._stable_timer_monotonic is not None:
                elapsed = max(0.0, current_monotonic - self._stable_timer_monotonic)
            expected = self._stable_timer_seconds + elapsed
            max_lag = float(self.config.phase.get("timer_stable_max_lag_seconds", 0.75))
            max_lead = float(self.config.phase.get("timer_stable_max_lead_seconds", 0.75))
            delta_from_expected = timer_seconds - expected
            if -max_lag <= delta_from_expected <= max_lead:
                adjusted = timer_seconds
                decision = "accept_ocr"
            else:
                adjusted = expected
                decision = "reject_ocr_lag" if delta_from_expected < 0 else "reject_ocr_lead"
            self._stable_timer_seconds = adjusted
            self._stable_timer_monotonic = current_monotonic
            if adjusted == timer_seconds:
                self._last_timer_ocr_seconds = timer_seconds
            self.last_timer_stabilizer = {
                "decision": decision,
                "ocr_seconds": round(timer_seconds, 3),
                "expected_seconds": round(expected, 3),
                "adjusted_seconds": round(adjusted, 3),
                "delta_seconds": round(delta_from_expected, 3),
                "max_lag_seconds": max_lag,
                "max_lead_seconds": max_lead,
            }
            return adjusted

        if self._stable_timer_seconds is None or self._stable_timer_monotonic is None:
            self.last_timer_stabilizer = {"decision": "missing"}
            return None
        max_extrapolate = float(self.config.phase.get("timer_max_extrapolate_seconds", 1.5))
        elapsed = max(0.0, current_monotonic - self._stable_timer_monotonic)
        adjusted = self._stable_timer_seconds + min(elapsed, max_extrapolate)
        self.last_timer_stabilizer = {
            "decision": "fallback_extrapolate",
            "seconds": round(adjusted, 3),
        }
        return adjusted

    def mark_action_executed(
        self,
        action_type: str,
        item_id: str | None,
        now_seconds: float | None = None,
    ) -> None:
        if action_type == "cast_skill" and item_id:
            if now_seconds is None:
                now_seconds = time.monotonic() - self.start_monotonic
            self.last_skill_cast[item_id] = float(now_seconds)

    def _read_phase(self, now_seconds: float) -> Phase:
        mode = self.config.phase.get("mode", "time")
        if mode != "time":
            return Phase.UNKNOWN
        placement = float(self.config.phase.get("placement_seconds", 120))
        extra = float(self.config.phase.get("extra_place_seconds", 10))
        if now_seconds < placement:
            return Phase.PLACEMENT
        if now_seconds < placement + extra:
            return Phase.EXTRA_PLACE
        return Phase.BATTLE

    def _read_match_timer_seconds(self, image: Any, offset_y: int = 0) -> float | None:
        self.last_timer_read_source = None
        if not bool(self.config.phase.get("read_screen_timer", True)):
            return None
        interval_seconds = max(0.0, float(self.config.phase.get("timer_ocr_interval_seconds", 0.0)))
        if (
            interval_seconds > 0
            and self._stable_timer_seconds is not None
            and self._stable_timer_monotonic is not None
            and self._last_timer_ocr_monotonic is not None
        ):
            current_monotonic = time.monotonic()
            elapsed_since_ocr = max(0.0, current_monotonic - self._last_timer_ocr_monotonic)
            if elapsed_since_ocr < interval_seconds:
                elapsed_since_stable = max(0.0, current_monotonic - self._stable_timer_monotonic)
                max_extrapolate = float(self.config.phase.get("timer_max_extrapolate_seconds", 1.5))
                seconds = self._stable_timer_seconds + min(elapsed_since_stable, max_extrapolate)
                self.last_timer_attempts.append(
                    {
                        "rect": None,
                        "seconds": round(seconds, 3),
                        "source": "extrapolated",
                        "elapsed_seconds": round(elapsed_since_ocr, 3),
                    }
                )
                self.last_timer_read_source = "extrapolated"
                return seconds
        rects = self._timer_rects_for_offset(offset_y)
        if not rects:
            return None
        digits_dir = self.config.root / self.config.phase.get("timer_digits_dir", "templates/timer_digits")
        for rect in rects:
            try:
                seconds = read_match_timer_seconds(self._cached_image_source(image), rect, digits_dir=digits_dir)
            except Exception as exc:
                seconds = None
                self.last_timer_attempts.append({"rect": rect, "seconds": None, "error": str(exc)})
                continue
            if seconds is not None:
                self._last_timer_ocr_monotonic = time.monotonic()
                self.last_timer_attempts.append({"rect": rect, "seconds": seconds})
                self.last_timer_read_source = "ocr"
                return seconds
            self.last_timer_attempts.append({"rect": rect, "seconds": seconds})
        return None

    def _read_cost(self, image: Any, offset_y: int = 0) -> int:
        mode = self.config.cost.get("mode", "debug_fixed")
        if mode == "debug_fixed":
            return int(self.config.cost.get("debug_fixed_value", 0))
        image_source = self._cached_image_source(image)
        if mode == "manual_file":
            path = Path(self.config.cost.get("manual_file", ""))
            if not path.is_absolute():
                path = self.config.root / path
            try:
                return int(path.read_text(encoding="utf-8").strip())
            except FileNotFoundError:
                return 0
        if mode == "digit_templates":
            rect = tuple(self.config.cost.get("rect", [0, 0, 0, 0]))
            if offset_y:
                rect = _offset_rect(rect, 0, offset_y)
            digits_dir = self.config.root / self.config.cost.get("digit_templates_dir", "templates/digits")
            return read_number_by_digit_templates(image_source, rect, digits_dir, self.config.digit_threshold())
        if mode == "command_value":
            digits_dir = self.config.root / self.config.cost.get("command_digits_dir", "templates/command_digits")
            for name, rect in self._cost_rects_for_offset(offset_y):
                value = read_command_value(image_source, rect, digits_dir)
                self.last_cost_attempts.append({"name": name, "rect": rect, "value": value})
                if value:
                    return value
            return 0
        raise RuntimeError(f"unsupported cost mode: {mode}")

    def _select_layout_offset_y(self, image: Any) -> int:
        offsets = self.config.screen.get("layout_offset_y_candidates", [0])
        if not isinstance(offsets, list) or not offsets:
            offsets = [0]
        candidates = [int(item) for item in offsets]
        if 0 not in candidates:
            candidates.insert(0, 0)

        fixed_offset = self.config.screen.get("layout_offset_y_fixed")
        if fixed_offset is not None:
            try:
                selected = int(fixed_offset)
            except (TypeError, ValueError):
                selected = 0
            self.last_layout_offset_y = selected
            self.last_layout_scores = {selected: self._score_hand_layout(image, selected)}
            return selected

        if bool(self.config.screen.get("layout_offset_fast_reuse", True)) and self.last_layout_offset_y in candidates:
            previous_offset = self.last_layout_offset_y
            previous_score = self._score_hand_layout(image, previous_offset)
            min_score = float(self.config.screen.get("layout_offset_min_switch_score", 4.0))
            if previous_score >= min_score:
                self.last_layout_scores = {previous_offset: previous_score}
                return previous_offset

        best_offset = candidates[0]
        best_score = -1.0
        scores: dict[int, float] = {}
        for offset_y in candidates:
            score = self._score_hand_layout(image, offset_y)
            scores[offset_y] = score
            if score > best_score:
                best_score = score
                best_offset = offset_y
        self.last_layout_scores = scores
        zero_score = scores.get(0)
        if zero_score is not None and best_offset != 0:
            sticky_margin = float(self.config.screen.get("layout_offset_sticky_margin", 1.0))
            min_score = float(self.config.screen.get("layout_offset_min_switch_score", 4.0))
            if best_score < min_score or best_score - zero_score <= sticky_margin:
                best_offset = 0
        self.last_layout_offset_y = best_offset
        return best_offset

    def _score_hand_layout(self, image: Any, offset_y: int) -> float:
        score = 0.0
        cache = self._frame_cache_for(image)
        for slot in self._hand_slots_for_offset(offset_y):
            cached_score = cache.layout_scores.get(slot.rect) if cache is not None else None
            if cached_score is None:
                if cache is not None:
                    cached_score = hand_slot_layout_score_array(self.config, cache.rgb_roi(slot.rect))
                    cache.layout_scores[slot.rect] = cached_score
                else:
                    cached_score = hand_slot_layout_score(self.config, image, slot.rect)
            score += cached_score * 0.4
            if self._slot_looks_playable(image, slot.rect):
                score += 1.5
        return score

    def _hand_slots_for_offset(self, offset_y: int) -> list[SlotConfig]:
        if offset_y == 0:
            return self.config.hand_slots
        return [
            replace(
                slot,
                rect=_offset_rect(slot.rect, 0, offset_y),
                click=_offset_point(slot.click, 0, offset_y),
            )
            for slot in self.config.hand_slots
        ]

    def _reserve_slot_for_offset(self, offset_y: int) -> SlotConfig | None:
        if not self.config.reserve_slot:
            return None
        if offset_y == 0:
            return self.config.reserve_slot
        return replace(
            self.config.reserve_slot,
            rect=_offset_rect(self.config.reserve_slot.rect, 0, offset_y),
            click=_offset_point(self.config.reserve_slot.click, 0, offset_y),
        )

    def _timer_rects_for_offset(self, offset_y: int = 0) -> list[tuple[int, int, int, int]]:
        raw_rects = list(self.config.phase.get("timer_rects", []))
        if not raw_rects and self.config.phase.get("timer_rect"):
            raw_rects.append(self.config.phase.get("timer_rect"))
        if not raw_rects:
            for item in self.config.data.get("calibration_regions", []):
                if item.get("name") == "top_timer":
                    raw_rects.append(item.get("rect"))
                    break

        extra_candidates = bool(self.config.phase.get("timer_extra_candidates", True))
        if not extra_candidates and raw_rects:
            raw_rects = raw_rects[:1]

        rects: list[tuple[int, int, int, int]] = []
        y_offsets: list[int] = [0]
        if extra_candidates:
            for item in self.config.phase.get("timer_y_offsets", []):
                try:
                    y_offsets.append(int(item))
                except Exception:
                    continue

        for rect_value in raw_rects:
            try:
                base = tuple(int(v) for v in rect_value)
            except Exception:
                continue
            for dy in y_offsets:
                rects.append(_offset_rect(base, 0, dy))
            if extra_candidates and base[1] <= 30:
                for top in (0, 2, 4, 8):
                    rects.append((base[0], top, base[2], base[3]))
                    rects.append((max(0, base[0] - 30), top, base[2], base[3]))

        return _unique_rects(rects)

    def _cost_rects_for_offset(self, offset_y: int = 0) -> list[tuple[str, tuple[int, int, int, int]]]:
        ordered: list[tuple[str, tuple[int, int, int, int]]] = []
        for name, rect_value in (
            ("number", self.config.cost.get("number_rect")),
            ("area", self.config.cost.get("rect")),
        ):
            if not rect_value:
                continue
            try:
                rect = tuple(int(v) for v in rect_value)
            except Exception:
                continue
            ordered.append((name, rect))

        seen: set[tuple[int, int, int, int]] = set()
        unique: list[tuple[str, tuple[int, int, int, int]]] = []
        for name, rect in ordered:
            if rect in seen:
                continue
            seen.add(rect)
            unique.append((name, rect))
        return unique

    def _read_hand_slot_playable(self, image: Any, offset_y: int = 0) -> dict[str, bool]:
        return {slot.name: self._slot_looks_playable(image, slot.rect) for slot in self._hand_slots_for_offset(offset_y)}

    def _read_cards(
        self,
        image: Any,
        hand_slot_playable: dict[str, bool] | None = None,
        offset_y: int = 0,
        current_cost: int | None = None,
    ) -> list[VisibleCard]:
        visible: list[VisibleCard] = []
        if hand_slot_playable is None:
            hand_slot_playable = self._read_hand_slot_playable(image, offset_y)
        image_source: Any | None = None
        now_monotonic = time.monotonic()
        diagnostics: dict[str, Any] = {
            "cache_enabled": bool(self.config.screen.get("card_match_cache", True)),
            "full_match_fallback": bool(self.config.screen.get("card_full_match_fallback", True)),
            "interval_seconds": self._hand_card_read_interval_seconds(),
            "interval_frames": self._hand_card_read_interval_frames(),
            "active_card_ids": None,
            "active_card_count": len(self.config.cards),
            "title_template_count": 0,
            "cache_hits": 0,
            "negative_cache_hits": 0,
            "title_reads": 0,
            "full_match_reads": 0,
            "slots": {},
        }
        active_card_ids = self._active_card_ids()
        title_templates = self._get_card_title_templates()
        if active_card_ids is not None:
            diagnostics["active_card_ids"] = sorted(active_card_ids)
            diagnostics["active_card_count"] = len(active_card_ids)
        diagnostics["title_template_count"] = sum(len(paths) for paths in title_templates.values())
        for slot in self._hand_slots_for_offset(offset_y):
            playable = bool(hand_slot_playable.get(slot.name, False))
            fingerprint = self._slot_fingerprint(image, slot.rect)
            cached, cache_reason = self._cached_card_identity_for_slot(
                slot,
                fingerprint=fingerprint,
                offset_y=offset_y,
                now_monotonic=now_monotonic,
            )
            slot_diag: dict[str, Any] = {
                "playable": playable,
                "cache_reason": cache_reason,
                "visible": False,
            }
            if cached is not None:
                card_id = cached.get("card_id")
                slot_diag["source"] = "cache" if card_id is not None else "negative_cache"
                if card_id is None:
                    diagnostics["negative_cache_hits"] += 1
                else:
                    diagnostics["cache_hits"] += 1
            else:
                if image_source is None:
                    image_source = self._cached_image_source(image)
                cached, read_source = self._read_card_identity_for_slot(
                    image=image,
                    image_source=image_source,
                    slot=slot,
                    offset_y=offset_y,
                    fingerprint=fingerprint,
                    now_monotonic=now_monotonic,
                )
                slot_diag["source"] = read_source
                if read_source == "title":
                    diagnostics["title_reads"] += 1
                elif read_source == "full_match":
                    diagnostics["full_match_reads"] += 1

            card_id = cached.get("card_id") if cached is not None else None
            slot_diag["card_id"] = card_id
            slot_diag["confidence"] = round(float(cached.get("confidence", 0.0)), 4) if cached is not None else 0.0
            if not card_id:
                diagnostics["slots"][slot.name] = slot_diag
                continue
            card = self.config.cards.get(str(card_id))
            if card is None:
                diagnostics["slots"][slot.name] = slot_diag
                continue
            if not playable:
                slot_diag["hidden_reason"] = "not_playable"
                diagnostics["slots"][slot.name] = slot_diag
                continue
            if current_cost is not None and current_cost < card.cost:
                slot_diag["hidden_reason"] = "unaffordable"
                diagnostics["slots"][slot.name] = slot_diag
                continue
            visible_card = VisibleCard(card=card, slot=slot, confidence=float(cached.get("confidence", 1.0)))
            visible.append(visible_card)
            slot_diag["visible"] = True
            diagnostics["slots"][slot.name] = slot_diag
        self.last_hand_card_diagnostics = diagnostics
        return visible

    def _hand_card_read_interval_frames(self) -> int:
        try:
            return max(0, int(self.config.screen.get("hand_card_read_interval_frames", 0)))
        except (TypeError, ValueError):
            return 0

    def _hand_card_read_interval_seconds(self) -> float:
        try:
            return max(0.0, float(self.config.screen.get("hand_card_read_interval_seconds", 0.0)))
        except (TypeError, ValueError):
            return 0.0

    def _cached_card_identity_for_slot(
        self,
        slot: SlotConfig,
        fingerprint: tuple[int, ...],
        offset_y: int,
        now_monotonic: float,
    ) -> tuple[dict[str, Any] | None, str]:
        if not bool(self.config.screen.get("card_match_cache", True)):
            return None, "cache_disabled"
        cached = self._slot_card_cache.get(slot.name)
        if not cached:
            return None, "missing"
        card_id = cached.get("card_id")
        active_card_ids = self._active_card_ids()
        if card_id is not None and active_card_ids is not None and str(card_id) not in active_card_ids:
            return None, "filtered_card"
        if tuple(cached.get("fingerprint", ())) != fingerprint:
            return None, "fingerprint_changed"
        if cached.get("offset_y") is not None and int(cached.get("offset_y")) != int(offset_y):
            return None, "offset_changed"
        due, reason = self._hand_card_cache_due(cached, now_monotonic)
        if due:
            return None, reason
        return cached, "fresh"

    def _hand_card_cache_due(self, cached: dict[str, Any], now_monotonic: float) -> tuple[bool, str]:
        frame_interval = self._hand_card_read_interval_frames()
        if frame_interval > 0:
            read_frame = cached.get("read_frame")
            if read_frame is None:
                return True, "frame_interval_missing"
            if self._frame_cache_index - int(read_frame) >= frame_interval:
                return True, "frame_interval"

        seconds_interval = self._hand_card_read_interval_seconds()
        if seconds_interval > 0:
            read_monotonic = cached.get("read_monotonic")
            if read_monotonic is None:
                return True, "time_interval_missing"
            if now_monotonic - float(read_monotonic) >= seconds_interval:
                return True, "time_interval"
        return False, "fresh"

    def _read_card_identity_for_slot(
        self,
        image: Any,
        image_source: Any,
        slot: SlotConfig,
        offset_y: int,
        fingerprint: tuple[int, ...],
        now_monotonic: float,
    ) -> tuple[dict[str, Any], str]:
        match = self._match_card_title(image, slot.rect)
        source = "title"
        if match is None and bool(self.config.screen.get("card_full_match_fallback", True)):
            match = best_template_match(
                image=image_source,
                search_rect=slot.rect,
                templates=self._get_card_templates(),
                threshold=self.config.card_threshold(),
            )
            source = "full_match"
        if match and match.item_id in self.config.cards:
            cached = {
                "fingerprint": fingerprint,
                "card_id": match.item_id,
                "confidence": match.confidence,
                "offset_y": offset_y,
                "read_frame": self._frame_cache_index,
                "read_monotonic": now_monotonic,
            }
        else:
            cached = {
                "fingerprint": fingerprint,
                "card_id": None,
                "confidence": 0.0,
                "offset_y": offset_y,
                "read_frame": self._frame_cache_index,
                "read_monotonic": now_monotonic,
            }
        if bool(self.config.screen.get("card_match_cache", True)):
            self._slot_card_cache[slot.name] = cached
        return cached, source

    def _slot_fingerprint(self, image: Any, rect: tuple[int, int, int, int]) -> tuple[int, ...]:
        import numpy as np

        title_rect = card_title_rect_for_slot(self.config, rect)
        cache = self._frame_cache_for(image)
        if cache is not None:
            cached = cache.fingerprints.get(title_rect)
            if cached is not None:
                return cached
            rgb = cache.rgb_roi(title_rect)
            if rgb.size == 0:
                fingerprint: tuple[int, ...] = ()
            else:
                cv2 = _load_cv2()
                gray = cv2.cvtColor(np.ascontiguousarray(rgb), cv2.COLOR_RGB2GRAY)
                resized = cv2.resize(np.ascontiguousarray(gray), (16, 4), interpolation=cv2.INTER_AREA)
                fingerprint = tuple((resized.reshape(-1) // 16).astype(int).tolist())
            cache.fingerprints[title_rect] = fingerprint
            return fingerprint
        crop = crop_image(image, title_rect).convert("L").resize((16, 4))
        arr = np.asarray(crop, dtype=np.uint8)
        return tuple((arr.reshape(-1) // 16).astype(int).tolist())

    def _slot_looks_playable(self, image: Any, rect: tuple[int, int, int, int]) -> bool:
        cache = self._frame_cache_for(image)
        if cache is None:
            return slot_looks_playable(self.config, image, rect)
        cached = cache.playable.get(rect)
        if cached is not None:
            return cached
        result = slot_looks_playable_array(self.config, cache.rgb_roi(rect))
        cache.playable[rect] = result
        return result

    def _match_card_title(
        self,
        image: Any,
        rect: tuple[int, int, int, int],
        card_ids: Any | None = None,
    ) -> MatchResult | None:
        templates = self._get_card_title_templates()
        if card_ids is not None:
            allowed = set(card_ids)
            templates = {card_id: path for card_id, path in templates.items() if card_id in allowed}
        if not templates:
            return None
        return best_template_match_multi(
            image=self._cached_image_source(image),
            search_rect=card_title_rect_for_slot(self.config, rect),
            templates=templates,
            threshold=float(self.config.matcher.get("card_title_threshold", 0.72)),
        )

    def _get_card_title_templates(self) -> dict[str, list[str]]:
        active_card_ids = self._active_card_ids()
        active_key = None if active_card_ids is None else tuple(sorted(active_card_ids))
        directory_keys: list[str] = []
        for key, default in (
            ("card_title_live_templates_dir", "templates/card_titles_live"),
            ("card_title_templates_dir", "templates/card_titles"),
        ):
            configured = self.config.matcher.get(key, default)
            if configured:
                directory_keys.append(str((self.config.root / str(configured)).resolve()))
        cache_key = (tuple(directory_keys), active_key)
        if self._card_title_templates is not None and self._card_title_templates_key == cache_key:
            return self._card_title_templates
        directories = [Path(text) for text in directory_keys]

        templates: dict[str, list[str]] = {}
        for card_id in self.config.cards:
            if active_card_ids is not None and card_id not in active_card_ids:
                continue
            paths: list[str] = []
            for directory in directories:
                if not directory.exists():
                    continue
                single = directory / f"{card_id}.png"
                if single.exists():
                    paths.append(str(single))
                paths.extend(str(path) for path in sorted(directory.glob(f"{card_id}_*.png")))
            if paths:
                templates[card_id] = paths
        self._card_title_templates = templates
        self._card_title_templates_key = cache_key
        return self._card_title_templates

    def _read_reserve(self, image: Any, offset_y: int = 0) -> str | None:
        slot = self._reserve_slot_for_offset(offset_y)
        if not slot:
            return None
        match = best_template_match(
            image=self._cached_image_source(image),
            search_rect=slot.rect,
            templates=self._get_card_templates(),
            threshold=self.config.card_threshold(),
        )
        return match.item_id if match else None

    def _get_card_templates(self) -> dict[str, str]:
        active_card_ids = self._active_card_ids()
        active_key = None if active_card_ids is None else tuple(sorted(active_card_ids))
        cache_key = (str(self.config.path), active_key)
        if self._card_templates is not None and self._card_templates_key == cache_key:
            return self._card_templates
        self._card_templates = {
            card.id: card.template
            for card in self.config.cards.values()
            if active_card_ids is None or card.id in active_card_ids
        }
        self._card_templates_key = cache_key
        return self._card_templates

    def _active_card_ids(self) -> set[str] | None:
        raw = self.config.matcher.get("active_card_ids")
        if raw is None:
            return None
        if isinstance(raw, str):
            values = [item.strip() for item in raw.split(",")]
        elif isinstance(raw, (list, tuple, set)):
            values = [str(item).strip() for item in raw]
        else:
            return None
        return {item for item in values if item and item in self.config.cards}

    def _get_skill_templates(self) -> dict[str, str]:
        return {
            skill.id: skill.template
            for skill in self.config.skills
            if skill.active and skill.template
        }

    def _read_skills(self, image: Any, now_seconds: float) -> list[SkillState]:
        states: list[SkillState] = []
        skill_templates = self._get_skill_templates()
        image_source = self._cached_image_source(image)
        for skill in self.config.skills:
            if not skill.active:
                states.append(SkillState(skill=skill, ready=False, confidence=0.0))
                continue

            cooldown_ready = True
            if skill.id in self.last_skill_cast:
                cooldown_ready = now_seconds - self.last_skill_cast[skill.id] >= skill.cooldown_seconds

            visual_ready = True
            confidence = 0.0
            ready_mode = skill.ready_mode
            diagnostics: dict[str, Any] = {"identity_source": "configured_slot", "ready_mode": ready_mode}
            if skill.template and skill.rect:
                match = best_template_match(
                    image=image_source,
                    search_rect=skill.rect,
                    templates={skill.id: skill.template},
                    threshold=0.0,
                )
                confidence = max(0.0, match.confidence) if match else 0.0
                diagnostics["template_score"] = round(confidence, 4)
                best_match = best_template_match(
                    image=image_source,
                    search_rect=skill.rect,
                    templates=skill_templates,
                    threshold=0.0,
                )
                if best_match:
                    diagnostics["best_template_id"] = best_match.item_id
                    diagnostics["best_template_score"] = round(max(0.0, best_match.confidence), 4)
                visual_diag = skill_slot_visual_diagnostics(image_source, skill.rect)
                diagnostics.update(visual_diag)
                if ready_mode in {"slot_visual_and_cooldown", "cooldown_only"}:
                    slot_visual_ready = skill_slot_looks_ready(self.config, visual_diag)
                    diagnostics["visual_ready"] = slot_visual_ready
                    diagnostics["visual_ready_reason"] = _skill_visual_ready_reason(self.config, visual_diag)
                    diagnostics["ready_max_dark_ratio"] = self.config.skill_ready_max_dark_ratio()
                    diagnostics["ready_min_brightness"] = self.config.skill_ready_min_brightness()
                    if ready_mode == "slot_visual_and_cooldown":
                        visual_ready = slot_visual_ready
                elif ready_mode != "cooldown_only":
                    visual_ready = confidence >= self.config.skill_threshold()

            states.append(
                SkillState(
                    skill=skill,
                    ready=cooldown_ready and visual_ready,
                    confidence=confidence,
                    seconds_since_cast=(
                        now_seconds - self.last_skill_cast[skill.id]
                        if skill.id in self.last_skill_cast
                        else None
                    ),
                    diagnostics=diagnostics,
                )
            )
        return states

    def _read_battlefield_targets(self, image: Any, phase: Phase) -> list[BattlefieldTarget]:
        battlefield = self.config.data.get("battlefield", {})
        self.last_battlefield_diagnostics = {"enabled": bool(battlefield.get("targets", [])), "targets": {}}
        if phase != Phase.BATTLE and not bool(battlefield.get("always_read", False)):
            self.last_battlefield_diagnostics["skipped_reason"] = "not_battle_phase"
            return []

        targets: list[BattlefieldTarget] = []
        for item in battlefield.get("targets", []):
            target_id = str(item["id"])
            template = _resolve_runtime_path(self.config.root, item.get("template"))
            search_rect, search_diagnostics = self._battlefield_search_rect(item)
            self.last_battlefield_diagnostics["targets"][target_id] = search_diagnostics
            if not template or search_rect[2] <= 0 or search_rect[3] <= 0:
                search_diagnostics["skipped_reason"] = "missing_template_or_rect"
                continue

            matches = template_matches(
                image=self._cached_image_source(image),
                search_rect=search_rect,
                templates={target_id: str(template)},
                threshold=float(item.get("threshold", self.config.battlefield_threshold())),
                min_distance=int(item.get("min_distance", 60)),
                max_matches=int(item.get("max_matches", 8)),
            )
            search_diagnostics["match_count"] = len(matches)
            search_diagnostics["matches"] = [
                {
                    "confidence": round(match.confidence, 4),
                    "rect": list(match.rect),
                    "click": list(_match_click(match.rect, item.get("click_offset"))),
                }
                for match in matches
            ]
            for match in matches:
                click = _match_click(match.rect, item.get("click_offset"))
                targets.append(
                    BattlefieldTarget(
                        target_id=target_id,
                        name=str(item.get("name", target_id)),
                        confidence=match.confidence,
                        rect=match.rect,
                        click=click,
                    )
                )
        return targets

    def _battlefield_search_rect(self, item: dict[str, Any]) -> tuple[tuple[int, int, int, int], dict[str, Any]]:
        target_id = str(item.get("id", "battlefield_target"))
        mode = str(item.get("search_mode", "configured_rect"))
        fallback_rect = tuple(int(v) for v in item.get("search_rect", [0, 0, 0, 0]))
        if mode != "historical_row":
            return fallback_rect, {"mode": "configured_rect", "search_rect": list(fallback_rect)}

        cache_key = json.dumps(
            {
                "target_id": target_id,
                "mode": mode,
                "fallback_rect": fallback_rect,
                "sources": item.get("historical_sources", []),
                "min_time": item.get("historical_min_time_seconds", 130),
                "row_x": item.get("row_scan_x", fallback_rect[0]),
                "row_width": item.get("row_scan_width", fallback_rect[2]),
                "padding_y": item.get("historical_padding_y", 80),
            },
            sort_keys=True,
            default=str,
        )
        cached = self._battlefield_search_rect_cache.get(cache_key)
        if cached is not None:
            rect, diagnostics = cached
            return rect, {**json.loads(json.dumps(diagnostics, default=str)), "cached": True}

        rect, diagnostics = self._build_historical_battlefield_row_rect(target_id, item, fallback_rect)
        self._battlefield_search_rect_cache[cache_key] = (rect, json.loads(json.dumps(diagnostics, default=str)))
        return rect, json.loads(json.dumps(diagnostics, default=str))

    def _build_historical_battlefield_row_rect(
        self,
        target_id: str,
        item: dict[str, Any],
        fallback_rect: tuple[int, int, int, int],
    ) -> tuple[tuple[int, int, int, int], dict[str, Any]]:
        min_time = float(item.get("historical_min_time_seconds", 130))
        padding_y = int(item.get("historical_padding_y", 80))
        row_x = int(item.get("row_scan_x", fallback_rect[0]))
        row_width = int(item.get("row_scan_width", fallback_rect[2]))
        rects: list[tuple[int, int, int, int]] = []
        frames_seen = 0
        frames_used = 0
        frames_without_time = 0
        manifests_used: list[str] = []

        for source in item.get("historical_sources", []):
            source_path = _resolve_runtime_path(self.config.root, source)
            if source_path is None:
                continue
            manifest_paths = _historical_manifest_paths(source_path)
            for manifest_path in manifest_paths:
                if not manifest_path.exists():
                    continue
                manifest_used = False
                try:
                    handle = manifest_path.open("r", encoding="utf-8")
                except OSError:
                    continue
                with handle:
                    for line in handle:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            record = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        frames_seen += 1
                        seconds = _historical_record_seconds(record)
                        if seconds is None:
                            frames_without_time += 1
                            continue
                        if seconds < min_time:
                            continue
                        if str(record.get("state", {}).get("phase", "")).lower() != "battle":
                            continue
                        frame_rects = _historical_target_rects(record, target_id)
                        if not frame_rects:
                            continue
                        rects.extend(frame_rects)
                        frames_used += 1
                        manifest_used = True
                if manifest_used:
                    manifests_used.append(str(manifest_path))

        if not rects:
            return fallback_rect, {
                "mode": "historical_row",
                "source": "fallback_config",
                "search_rect": list(fallback_rect),
                "historical_min_time_seconds": min_time,
                "frames_seen": frames_seen,
                "frames_used": frames_used,
                "frames_without_time": frames_without_time,
                "sample_rect_count": 0,
            }

        min_top = min(rect[1] for rect in rects)
        max_bottom = max(rect[1] + rect[3] for rect in rects)
        top = max(0, min_top - padding_y)
        height = max(1, (max_bottom - min_top) + padding_y * 2)
        search_rect = (row_x, top, row_width, height)
        xs = [rect[0] for rect in rects]
        ys = [rect[1] for rect in rects]
        return search_rect, {
            "mode": "historical_row",
            "source": "historical_samples",
            "search_rect": list(search_rect),
            "historical_min_time_seconds": min_time,
            "frames_seen": frames_seen,
            "frames_used": frames_used,
            "frames_without_time": frames_without_time,
            "sample_rect_count": len(rects),
            "sample_x_range": [min(xs), max(xs)],
            "sample_y_range": [min(ys), max(ys)],
            "manifests_used": manifests_used,
        }

    def _frame_cache_for(self, image: Any) -> _FrameCache | None:
        cache = self._active_frame_cache
        if cache is None or not cache.matches(image):
            return None
        return cache

    def _cached_image_source(self, image: Any) -> Any:
        return self._frame_cache_for(image) or image


def normalize_capture_image(image: Any, config: BotConfig) -> tuple[Any, tuple[float, float]]:
    reference_size = config.screen.get("reference_size")
    if not isinstance(reference_size, list) or len(reference_size) != 2:
        return image, (1.0, 1.0)

    target_width = int(reference_size[0])
    target_height = int(reference_size[1])
    if target_width <= 0 or target_height <= 0:
        return image, (1.0, 1.0)

    width, height = image.size
    scale = (width / target_width, height / target_height)
    if width == target_width and height == target_height:
        return image, scale

    return image.resize((target_width, target_height)), scale


def crop_image(image: Any, rect: tuple[int, int, int, int]) -> Any:
    if isinstance(image, _FrameCache):
        return _pil_image_from_rgb_array(image.rgb_roi(rect))
    left, top, width, height = rect
    return image.crop((left, top, left + width, top + height))


def _array_roi(array: Any, rect: tuple[int, int, int, int]) -> Any:
    left, top, width, height = (int(v) for v in rect)
    if width <= 0 or height <= 0:
        return array[0:0, 0:0]
    array_height, array_width = array.shape[:2]
    x1 = max(0, min(array_width, left))
    y1 = max(0, min(array_height, top))
    x2 = max(x1, min(array_width, left + width))
    y2 = max(y1, min(array_height, top + height))
    return array[y1:y2, x1:x2]


def _pil_image_from_rgb_array(array: Any) -> Any:
    import numpy as np

    Image = _load_pil_image()
    return Image.fromarray(np.ascontiguousarray(array), "RGB")


def _offset_rect(rect: tuple[int, int, int, int], dx: int = 0, dy: int = 0) -> tuple[int, int, int, int]:
    return rect[0] + dx, rect[1] + dy, rect[2], rect[3]


def _offset_point(point: tuple[int, int], dx: int = 0, dy: int = 0) -> tuple[int, int]:
    return point[0] + dx, point[1] + dy


def _unique_rects(rects: list[tuple[int, int, int, int]]) -> list[tuple[int, int, int, int]]:
    seen: set[tuple[int, int, int, int]] = set()
    unique: list[tuple[int, int, int, int]] = []
    for rect in rects:
        if rect in seen:
            continue
        seen.add(rect)
        unique.append(rect)
    return unique


def hand_slot_layout_score(config: BotConfig, image: Any, rect: tuple[int, int, int, int]) -> float:
    if isinstance(image, _FrameCache):
        return hand_slot_layout_score_array(config, image.rgb_roi(rect))
    import numpy as np

    region = crop_image(image, rect).convert("RGB")
    arr = np.asarray(region, dtype=np.uint8)
    return hand_slot_layout_score_array(config, arr)


def hand_slot_layout_score_array(config: BotConfig, arr: Any) -> float:
    import numpy as np

    if arr.size == 0:
        return 0.0
    height, width = arr.shape[:2]
    title = arr[0 : max(1, int(height * 0.32)), 0 : max(1, int(width * 0.78))]
    skill = arr[int(height * 0.55) : height, 0 : max(1, int(width * 0.65))]
    cost = arr[0 : max(1, int(height * 0.38)), int(width * 0.62) : width]

    title_bright = float((title.max(axis=2) > 95).mean()) if title.size else 0.0
    skill_bright = float((skill.max(axis=2) > 120).mean()) if skill.size else 0.0
    red = cost[:, :, 0].astype(int) if cost.size else np.array([])
    green = cost[:, :, 1].astype(int) if cost.size else np.array([])
    blue = cost[:, :, 2].astype(int) if cost.size else np.array([])
    if cost.size:
        yellow = (
            (red >= int(config.matcher.get("playable_card_yellow_min_red", 150)))
            & (green >= int(config.matcher.get("playable_card_yellow_min_green", 105)))
            & (blue <= int(config.matcher.get("playable_card_yellow_max_blue", 95)))
            & ((red - blue) >= int(config.matcher.get("playable_card_yellow_min_red_blue_delta", 65)))
            & ((green - blue) >= int(config.matcher.get("playable_card_yellow_min_green_blue_delta", 45)))
        )
        yellow_ratio = float(yellow.mean())
    else:
        yellow_ratio = 0.0
    return title_bright * 1.8 + skill_bright + yellow_ratio * 3.0


def slot_looks_playable(config: BotConfig, image: Any, rect: tuple[int, int, int, int]) -> bool:
    if isinstance(image, _FrameCache):
        return slot_looks_playable_array(config, image.rgb_roi(rect))
    import numpy as np

    region = crop_image(image, rect).convert("RGB")
    arr = np.asarray(region, dtype=np.uint8)
    return slot_looks_playable_array(config, arr)


def slot_looks_playable_array(config: BotConfig, arr: Any) -> bool:
    height, width = arr.shape[:2]
    cost_region = arr[0 : max(1, int(height * 0.38)), int(width * 0.62) : width]
    if cost_region.size:
        red = cost_region[:, :, 0].astype(int)
        green = cost_region[:, :, 1].astype(int)
        blue = cost_region[:, :, 2].astype(int)
        yellow_mask = (
            (red >= int(config.matcher.get("playable_card_yellow_min_red", 150)))
            & (green >= int(config.matcher.get("playable_card_yellow_min_green", 105)))
            & (blue <= int(config.matcher.get("playable_card_yellow_max_blue", 95)))
            & ((red - blue) >= int(config.matcher.get("playable_card_yellow_min_red_blue_delta", 65)))
            & ((green - blue) >= int(config.matcher.get("playable_card_yellow_min_green_blue_delta", 45)))
        )
        yellow_ratio = float(yellow_mask.mean())
        if yellow_ratio >= float(config.matcher.get("playable_card_min_yellow_ratio", 0.04)):
            return True

    max_channel = arr.max(axis=2).astype(float)
    brightness = float(max_channel.mean())
    bright_ratio = float((max_channel > 85).mean())
    return brightness >= float(config.matcher.get("playable_card_min_brightness", 90)) or bright_ratio >= float(
        config.matcher.get("playable_card_min_bright_ratio", 0.35)
    )


def skill_slot_visual_diagnostics(image: Any, rect: tuple[int, int, int, int]) -> dict[str, float]:
    if isinstance(image, _FrameCache):
        arr = image.rgb_roi(rect)
    else:
        import numpy as np

        arr = np.asarray(crop_image(image, rect).convert("RGB"), dtype=np.uint8)
    return skill_slot_visual_diagnostics_array(arr)


def skill_slot_visual_diagnostics_array(arr: Any) -> dict[str, float]:
    import numpy as np

    if arr.size == 0:
        return {
            "brightness": 0.0,
            "dark_ratio": 1.0,
            "bright_ratio": 0.0,
            "saturation": 0.0,
        }
    height, width = arr.shape[:2]
    center = arr[int(height * 0.18) : int(height * 0.78), int(width * 0.18) : int(width * 0.82)]
    if center.size == 0:
        center = arr
    max_channel = center.max(axis=2).astype(float)
    min_channel = center.min(axis=2).astype(float)
    saturation = max_channel - min_channel
    return {
        "brightness": round(float(max_channel.mean()), 3),
        "dark_ratio": round(float((max_channel < 45).mean()), 4),
        "bright_ratio": round(float((max_channel > 150).mean()), 4),
        "saturation": round(float(saturation.mean()), 3),
    }


def skill_slot_looks_ready(config: BotConfig, diagnostics: dict[str, float]) -> bool:
    dark_ratio = float(diagnostics.get("dark_ratio", 1.0))
    brightness = float(diagnostics.get("brightness", 0.0))
    return (
        dark_ratio <= config.skill_ready_max_dark_ratio()
        and brightness >= config.skill_ready_min_brightness()
    )


def _skill_visual_ready_reason(config: BotConfig, diagnostics: dict[str, float]) -> str:
    dark_ratio = float(diagnostics.get("dark_ratio", 1.0))
    brightness = float(diagnostics.get("brightness", 0.0))
    if dark_ratio > config.skill_ready_max_dark_ratio():
        return "cooldown_dark_overlay"
    if brightness < config.skill_ready_min_brightness():
        return "too_dim"
    return "slot_visual_ready"


def card_title_rect_for_slot(config: BotConfig, rect: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    return (
        rect[0] + int(config.matcher.get("card_title_offset_x", 18)),
        rect[1] + int(config.matcher.get("card_title_offset_y", -2)),
        int(config.matcher.get("card_title_width", 150)),
        int(config.matcher.get("card_title_height", 30)),
    )


def _resolve_runtime_path(root: Path, path_value: str | None) -> Path | None:
    if not path_value:
        return None
    path = Path(path_value)
    if path.is_absolute():
        return path
    return root / path


def _historical_manifest_paths(source_path: Path) -> list[Path]:
    if source_path.is_file():
        return [source_path]
    if source_path.name == "training_samples":
        return sorted(source_path.glob("battle_live_*/manifest.jsonl"))
    manifest = source_path / "manifest.jsonl"
    if manifest.exists():
        return [manifest]
    return sorted(source_path.glob("**/manifest.jsonl"))


def _historical_record_seconds(record: dict[str, Any]) -> float | None:
    state = record.get("state")
    candidates: list[Any] = []
    if isinstance(state, dict):
        candidates.append(state.get("time"))
    candidates.append(record.get("screen_timer_seconds"))
    for value in candidates:
        if isinstance(value, (int, float)):
            return float(value)
    return None


def _historical_target_rects(record: dict[str, Any], target_id: str) -> list[tuple[int, int, int, int]]:
    state = record.get("state")
    if not isinstance(state, dict):
        return []
    targets = state.get("battlefield_targets")
    if not isinstance(targets, list):
        return []
    rects: list[tuple[int, int, int, int]] = []
    for target in targets:
        if not isinstance(target, dict) or str(target.get("target_id")) != target_id:
            continue
        rect = target.get("rect")
        if not isinstance(rect, list) or len(rect) != 4:
            continue
        try:
            parsed = tuple(int(value) for value in rect)
        except (TypeError, ValueError):
            continue
        if parsed[2] > 0 and parsed[3] > 0:
            rects.append(parsed)  # type: ignore[arg-type]
    return rects


def _match_click(rect: tuple[int, int, int, int], click_offset: Any) -> tuple[int, int]:
    left, top, width, height = rect
    if isinstance(click_offset, list) and len(click_offset) == 2:
        return left + int(click_offset[0]), top + int(click_offset[1])
    return left + width // 2, top + height // 2


def best_template_match(
    image: Any,
    search_rect: tuple[int, int, int, int],
    templates: dict[str, str],
    threshold: float,
) -> MatchResult | None:
    cv2 = _load_cv2()
    import numpy as np

    if isinstance(image, _FrameCache):
        region_bgr = image.bgr_roi(search_rect)
    else:
        region = crop_image(image, search_rect)
        region_bgr = cv2.cvtColor(np.asarray(region.convert("RGB"), dtype=np.uint8), cv2.COLOR_RGB2BGR)
    best: MatchResult | None = None

    for item_id, template_path in templates.items():
        if not template_path:
            continue
        template = _cached_cv2_imread(template_path, cv2.IMREAD_COLOR)
        if template is None:
            continue
        if template.shape[0] > region_bgr.shape[0] or template.shape[1] > region_bgr.shape[1]:
            continue

        result = cv2.matchTemplate(region_bgr, template, cv2.TM_CCOEFF_NORMED)
        _min_val, max_val, _min_loc, max_loc = cv2.minMaxLoc(result)
        if max_val < threshold:
            continue

        left = search_rect[0] + int(max_loc[0])
        top = search_rect[1] + int(max_loc[1])
        rect = (left, top, int(template.shape[1]), int(template.shape[0]))
        if best is None or max_val > best.confidence:
            best = MatchResult(item_id=item_id, confidence=float(max_val), rect=rect)

    return best


def best_template_match_multi(
    image: Any,
    search_rect: tuple[int, int, int, int],
    templates: dict[str, list[str]],
    threshold: float,
) -> MatchResult | None:
    cv2 = _load_cv2()
    import numpy as np

    if isinstance(image, _FrameCache):
        region_bgr = image.bgr_roi(search_rect)
    else:
        region = crop_image(image, search_rect)
        region_bgr = cv2.cvtColor(np.asarray(region.convert("RGB"), dtype=np.uint8), cv2.COLOR_RGB2BGR)
    best: MatchResult | None = None

    for item_id, template_paths in templates.items():
        for template_path in template_paths:
            if not template_path:
                continue
            template = _cached_cv2_imread(template_path, cv2.IMREAD_COLOR)
            if template is None:
                continue
            if template.shape[0] > region_bgr.shape[0] or template.shape[1] > region_bgr.shape[1]:
                continue

            result = cv2.matchTemplate(region_bgr, template, cv2.TM_CCOEFF_NORMED)
            _min_val, max_val, _min_loc, max_loc = cv2.minMaxLoc(result)
            if max_val < threshold:
                continue

            left = search_rect[0] + int(max_loc[0])
            top = search_rect[1] + int(max_loc[1])
            rect = (left, top, int(template.shape[1]), int(template.shape[0]))
            if best is None or max_val > best.confidence:
                best = MatchResult(item_id=item_id, confidence=float(max_val), rect=rect)

    return best


def template_matches(
    image: Any,
    search_rect: tuple[int, int, int, int],
    templates: dict[str, str],
    threshold: float,
    min_distance: int = 60,
    max_matches: int = 8,
) -> list[MatchResult]:
    cv2 = _load_cv2()
    import numpy as np

    if isinstance(image, _FrameCache):
        region_bgr = image.bgr_roi(search_rect)
    else:
        region = crop_image(image, search_rect)
        region_bgr = cv2.cvtColor(np.asarray(region.convert("RGB"), dtype=np.uint8), cv2.COLOR_RGB2BGR)
    candidates: list[tuple[float, str, tuple[int, int, int, int]]] = []

    for item_id, template_path in templates.items():
        if not template_path:
            continue
        template = _cached_cv2_imread(template_path, cv2.IMREAD_COLOR)
        if template is None:
            continue
        if template.shape[0] > region_bgr.shape[0] or template.shape[1] > region_bgr.shape[1]:
            continue

        result = cv2.matchTemplate(region_bgr, template, cv2.TM_CCOEFF_NORMED)
        ys, xs = np.where(result >= threshold)
        for x, y in zip(xs, ys):
            left = search_rect[0] + int(x)
            top = search_rect[1] + int(y)
            rect = (left, top, int(template.shape[1]), int(template.shape[0]))
            candidates.append((float(result[y, x]), item_id, rect))

    candidates.sort(key=lambda item: item[0], reverse=True)
    selected: list[MatchResult] = []
    for confidence, item_id, rect in candidates:
        if any(_too_close(rect, existing.rect, min_distance) for existing in selected):
            continue
        selected.append(MatchResult(item_id=item_id, confidence=confidence, rect=rect))
        if len(selected) >= max_matches:
            break
    return selected


def _too_close(
    a: tuple[int, int, int, int],
    b: tuple[int, int, int, int],
    min_distance: int,
) -> bool:
    ax = a[0] + a[2] / 2
    ay = a[1] + a[3] / 2
    bx = b[0] + b[2] / 2
    by = b[1] + b[3] / 2
    return ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5 < min_distance


def read_number_by_digit_templates(
    image: Any,
    rect: tuple[int, int, int, int],
    digits_dir: Path,
    threshold: float,
) -> int:
    cv2 = _load_cv2()
    import numpy as np

    if isinstance(image, _FrameCache):
        region_bgr = image.bgr_roi(rect)
    else:
        region = crop_image(image, rect)
        region_bgr = cv2.cvtColor(np.asarray(region.convert("RGB"), dtype=np.uint8), cv2.COLOR_RGB2BGR)
    matches: list[tuple[int, int, float]] = []

    for digit in range(10):
        template_path = digits_dir / f"{digit}.png"
        template = _cached_cv2_imread(template_path, cv2.IMREAD_COLOR)
        if template is None:
            continue
        result = cv2.matchTemplate(region_bgr, template, cv2.TM_CCOEFF_NORMED)
        ys, xs = np.where(result >= threshold)
        for x, y in zip(xs, ys):
            matches.append((int(x), digit, float(result[y, x])))

    if not matches:
        return 0

    matches.sort(key=lambda item: (item[0], -item[2]))
    collapsed: list[tuple[int, int, float]] = []
    min_gap = 6
    for x, digit, score in matches:
        if not collapsed or x - collapsed[-1][0] > min_gap:
            collapsed.append((x, digit, score))
        elif score > collapsed[-1][2]:
            collapsed[-1] = (x, digit, score)

    value = "".join(str(digit) for _x, digit, _score in collapsed)
    return int(value) if value else 0


def read_command_value(image: Any, rect: tuple[int, int, int, int], digits_dir: Path | None = None) -> int:
    crop = crop_image(image, rect).convert("RGB")
    digit_zone = crop
    if crop.width > 120:
        # Full command-value crops include the "指挥值" label; the numeric
        # value sits to the right. Small configured number_rect crops are
        # already just the numeric area.
        digit_zone = crop.crop((118, 8, min(crop.width, 180), min(crop.height, 50)))
    value = read_number_by_connected_digit_templates(digit_zone, digits_dir or Path("templates/command_digits"))
    if value is None:
        value = read_number_by_multi_digit_templates(digit_zone, digits_dir or Path("templates/command_digits"), threshold=0.64)
    if value is None:
        value = read_segment_digits(crop)
    return value if value is not None else 0


def read_number_by_connected_digit_templates(image: Any, digits_dir: Path, threshold: float = 0.65) -> int | None:
    if not digits_dir.exists():
        return None
    import numpy as np

    templates = _load_connected_digit_templates(digits_dir)
    if not templates:
        return None
    mask = _command_digit_mask(image)
    boxes = _command_digit_component_boxes(mask)
    if not boxes:
        return None

    digits: list[str] = []
    for left, top, right, bottom in boxes:
        digit_mask = mask[top:bottom, left:right]
        result = _classify_digit_component(digit_mask, templates)
        if result is None:
            return None
        digit, score = result
        if digit == 1 and score >= 0.50:
            digits.append(str(digit))
            continue
        if score < threshold:
            return None
        digits.append(str(digit))
    return int("".join(digits)) if digits else None


def _load_connected_digit_templates(digits_dir: Path) -> list[tuple[int, Any]]:
    return list(_load_connected_digit_templates_cached(str(digits_dir.resolve())))


@lru_cache(maxsize=16)
def _load_connected_digit_templates_cached(digits_dir_text: str) -> tuple[tuple[int, Any], ...]:
    digits_dir = Path(digits_dir_text)
    templates: list[tuple[int, Any]] = []
    for path_text in _glob_template_paths(str(digits_dir.resolve()), "*.png"):
        path = Path(path_text)
        if not path.name[:1].isdigit():
            continue
        mask = _command_digit_mask(_load_pil_image().open(path).convert("RGB"))
        boxes = _command_digit_component_boxes(mask)
        if not boxes:
            continue
        left, top, right, bottom = max(
            boxes,
            key=lambda box: (box[2] - box[0]) * (box[3] - box[1]),
        )
        normalized = _normalize_digit_mask(mask[top:bottom, left:right])
        templates.append((int(path.name[0]), normalized))
    return tuple(templates)


def _command_digit_mask(image: Any) -> Any:
    import numpy as np

    rgb = np.array(image.convert("RGB"))
    gray = np.array(image.convert("L"))
    max_channel = rgb.max(axis=2)
    min_channel = rgb.min(axis=2)
    color_span = max_channel - min_channel
    return ((gray >= 115) & (color_span <= 95)) | ((max_channel >= 145) & (color_span <= 70))


def _command_digit_component_boxes(mask: Any) -> list[tuple[int, int, int, int]]:
    cv2 = _load_cv2()
    import numpy as np

    binary = np.asarray(mask, dtype=np.uint8)
    count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(binary, connectivity=8)
    boxes: list[tuple[int, int, int, int]] = []
    for label in range(1, count):
        x, y, w, h, area = stats[label]
        if area < 18 or h < 10 or w < 2:
            continue
        if y < 6:
            continue
        boxes.append((int(x), int(y), int(x + w), int(y + h)))
    boxes.sort(key=lambda box: box[0])
    return boxes


def _normalize_digit_mask(mask: Any, size: tuple[int, int] = (20, 28)) -> Any:
    cv2 = _load_cv2()
    import numpy as np

    arr = np.asarray(mask, dtype=np.uint8) * 255
    return cv2.resize(arr, size, interpolation=cv2.INTER_NEAREST) > 0


def _classify_digit_component(mask: Any, templates: list[tuple[int, Any]]) -> tuple[int, float] | None:
    import numpy as np

    arr = np.asarray(mask, dtype=bool)
    if arr.size:
        height, width = arr.shape
        fill_ratio = float(arr.mean())
        if width <= 5 and height >= 14 and fill_ratio >= 0.55:
            return 1, 0.80

    normalized = _normalize_digit_mask(mask)
    best_digit: int | None = None
    best_score = -1.0
    for digit, template in templates:
        intersection = float(np.logical_and(normalized, template).sum())
        union = float(np.logical_or(normalized, template).sum())
        score = intersection / union if union else 0.0
        if score > best_score:
            best_digit = digit
            best_score = score
    return (best_digit, best_score) if best_digit is not None else None


def read_number_by_multi_digit_templates(image: Any, digits_dir: Path, threshold: float = 0.55) -> int | None:
    if not digits_dir.exists():
        return None
    cv2 = _load_cv2()
    import numpy as np

    gray = np.array(image.convert("L"))
    matches: list[tuple[int, int, int, float]] = []
    digits_dir_text = str(digits_dir.resolve())
    for digit in range(10):
        for path in _glob_template_paths(digits_dir_text, f"{digit}*.png"):
            template = _cached_cv2_imread(path, cv2.IMREAD_GRAYSCALE)
            if template is None:
                continue
            if template.shape[0] > gray.shape[0] or template.shape[1] > gray.shape[1]:
                continue
            result = cv2.matchTemplate(gray, template, cv2.TM_CCOEFF_NORMED)
            ys, xs = np.where(result >= threshold)
            for x, y in zip(xs, ys):
                matches.append((int(x), digit, int(template.shape[1]), float(result[y, x])))
    if not matches:
        return None
    matches.sort(key=lambda item: (item[0], -item[3]))
    collapsed: list[tuple[int, int, int, float]] = []
    for x, digit, width, score in matches:
        if not collapsed or x - collapsed[-1][0] > 10:
            collapsed.append((x, digit, width, score))
        elif score > collapsed[-1][3]:
            collapsed[-1] = (x, digit, width, score)
    value = "".join(str(digit) for _x, digit, _width, _score in collapsed)
    return int(value) if value else None


def read_segment_digits(image: Any) -> int | None:
    import numpy as np

    gray = np.array(image.convert("L"))
    rgb = np.array(image.convert("RGB"))
    # Command-value digits are bright yellow/white on a dark bar. Keep bright
    # strokes and let connected components find each digit.
    max_channel = rgb.max(axis=2)
    min_channel = rgb.min(axis=2)
    color_span = max_channel - min_channel
    mask = (max_channel >= 120) & ((gray >= 120) | (color_span >= 35))
    boxes = _component_boxes(mask, min_area=8)
    if not boxes:
        return None
    boxes = _merge_digit_boxes(boxes)
    digits: list[int] = []
    for box in boxes:
        digit_mask = mask[box[1] : box[3], box[0] : box[2]]
        digit = _read_timer_digit(_pad_mask(digit_mask, 2))
        if digit is None:
            return None
        digits.append(digit)
    if not digits:
        return None
    return int("".join(str(digit) for digit in digits))


def _component_boxes(mask: Any, min_area: int = 8) -> list[tuple[int, int, int, int]]:
    cv2 = _load_cv2()
    import numpy as np

    binary = np.asarray(mask, dtype=np.uint8)
    count, labels, stats, _centroids = cv2.connectedComponentsWithStats(binary, connectivity=8)
    boxes: list[tuple[int, int, int, int]] = []
    for label in range(1, count):
        x, y, w, h, area = stats[label]
        if area < min_area or h < 8 or w < 2:
            continue
        boxes.append((int(x), int(y), int(x + w), int(y + h)))
    boxes.sort(key=lambda box: box[0])
    return boxes


def _merge_digit_boxes(boxes: list[tuple[int, int, int, int]]) -> list[tuple[int, int, int, int]]:
    if not boxes:
        return []
    merged: list[tuple[int, int, int, int]] = []
    for box in boxes:
        if not merged or box[0] - merged[-1][2] > 3:
            merged.append(box)
            continue
        prev = merged[-1]
        merged[-1] = (
            min(prev[0], box[0]),
            min(prev[1], box[1]),
            max(prev[2], box[2]),
            max(prev[3], box[3]),
        )
    return [box for box in merged if box[2] - box[0] >= 3 and box[3] - box[1] >= 10]


def _pad_mask(mask: Any, padding: int) -> Any:
    import numpy as np

    return np.pad(np.asarray(mask, dtype=bool), ((padding, padding), (padding, padding)), constant_values=False)


def read_match_timer_seconds(
    image: Any,
    rect: tuple[int, int, int, int],
    digits_dir: Path | None = None,
) -> float | None:
    import numpy as np

    region = crop_image(image, rect).convert("L")
    arr = np.array(region)
    # The battle timer uses a fixed gray seven-segment-like font on a dark bar.
    # A conservative threshold keeps the background out while preserving dim digits.
    threshold = max(45, min(90, int(arr.mean() + arr.std() * 0.35)))
    mask = arr >= threshold
    char_boxes = _timer_char_boxes(region.size)
    if len(char_boxes) != 8:
        return None

    chars: list[str] = []
    for index, box in enumerate(char_boxes):
        if index in (2, 5):
            chars.append(":")
            continue
        digit = _read_timer_digit_by_template(region.crop(box), digits_dir, char_index=index)
        if digit is None:
            digit = _read_timer_digit(mask[box[1] : box[3], box[0] : box[2]])
        if digit is None:
            return None
        chars.append(str(digit))

    if chars[2] != ":" or chars[5] != ":":
        return None
    if any(ch == "?" for ch in chars):
        return None
    hours = int("".join(chars[0:2]))
    minutes = int("".join(chars[3:5]))
    seconds = int("".join(chars[6:8]))
    if hours != 0 or minutes > 59 or seconds > 59:
        return None
    return float(hours * 3600 + minutes * 60 + seconds)


def _read_timer_digit_by_template(crop: Any, digits_dir: Path | None, char_index: int | None = None) -> int | None:
    if digits_dir is None or not digits_dir.exists():
        return None
    cv2 = _load_cv2()
    import numpy as np

    crop_arr = np.array(crop.convert("L"))
    best_digit: int | None = None
    best_score = -1.0
    for digit, template in _timer_digit_templates(
        str(digits_dir.resolve()),
        -1 if char_index is None else int(char_index),
        int(crop_arr.shape[1]),
        int(crop_arr.shape[0]),
    ):
        result = cv2.matchTemplate(crop_arr, template, cv2.TM_CCOEFF_NORMED)
        score = float(result[0, 0])
        if score > best_score:
            best_digit = digit
            best_score = score
    return best_digit if best_digit is not None and best_score >= 0.55 else None


@lru_cache(maxsize=128)
def _timer_digit_templates(
    digits_dir_text: str,
    char_index: int,
    width: int,
    height: int,
) -> tuple[tuple[int, Any], ...]:
    cv2 = _load_cv2()
    templates: list[tuple[int, Any]] = []
    for digit in range(10):
        paths = ()
        if char_index >= 0:
            paths = _glob_template_paths(digits_dir_text, f"{digit}*_{char_index}.png")
        if not paths:
            paths = _glob_template_paths(digits_dir_text, f"{digit}*.png")
        for path in paths:
            template = _cached_cv2_imread(path, cv2.IMREAD_GRAYSCALE)
            if template is None:
                continue
            if template.shape != (height, width):
                template = cv2.resize(template, (width, height))
            templates.append((digit, template))
    return tuple(templates)


def _timer_char_boxes(size: tuple[int, int]) -> list[tuple[int, int, int, int]]:
    width, height = size
    # Calibrated from the 1920x1080 top timer crop [912, 20, 93, 30].
    base_width = 93
    base_boxes = [
        (0, 0, 12, 30),
        (13, 0, 25, 30),
        (26, 0, 31, 30),
        (32, 0, 44, 30),
        (45, 0, 57, 30),
        (58, 0, 63, 30),
        (64, 0, 76, 30),
        (77, 0, 89, 30),
    ]
    sx = width / base_width
    sy = height / 30
    boxes: list[tuple[int, int, int, int]] = []
    for left, top, right, bottom in base_boxes:
        boxes.append(
            (
                max(0, min(width, int(round(left * sx)))),
                max(0, min(height, int(round(top * sy)))),
                max(0, min(width, int(round(right * sx)))),
                max(0, min(height, int(round(bottom * sy)))),
            )
        )
    return boxes


def _read_timer_digit(mask: Any) -> int | None:
    import numpy as np

    if mask.size == 0:
        return None
    mask = np.asarray(mask, dtype=bool)
    height, width = mask.shape
    if height < 8 or width < 6:
        return None

    segments = {
        "top": mask[0 : max(1, height // 5), :].mean(),
        "mid": mask[height * 2 // 5 : max(height * 2 // 5 + 1, height * 3 // 5), :].mean(),
        "bot": mask[height * 4 // 5 : height, :].mean(),
        "ul": mask[height // 5 : max(height // 5 + 1, height * 2 // 5), 0 : max(1, width // 3)].mean(),
        "ur": mask[height // 5 : max(height // 5 + 1, height * 2 // 5), width * 2 // 3 : width].mean(),
        "ll": mask[height * 3 // 5 : max(height * 3 // 5 + 1, height * 4 // 5), 0 : max(1, width // 3)].mean(),
        "lr": mask[height * 3 // 5 : max(height * 3 // 5 + 1, height * 4 // 5), width * 2 // 3 : width].mean(),
        "center": mask[height // 5 : max(height // 5 + 1, height * 4 // 5), width // 3 : max(width // 3 + 1, width * 2 // 3)].mean(),
    }
    active = {name for name, value in segments.items() if value >= 0.32}

    patterns = {
        0: {"top", "bot", "ul", "ur", "ll", "lr"},
        1: {"ur", "lr"},
        2: {"top", "mid", "bot", "ur", "ll"},
        3: {"top", "mid", "bot", "ur", "lr"},
        4: {"mid", "ul", "ur", "lr"},
        5: {"top", "mid", "bot", "ul", "lr"},
        6: {"top", "mid", "bot", "ul", "ll", "lr"},
        7: {"top", "ur", "lr"},
        8: {"top", "mid", "bot", "ul", "ur", "ll", "lr"},
        9: {"top", "mid", "bot", "ul", "ur", "lr"},
    }

    best_digit: int | None = None
    best_score = -999
    for digit, expected in patterns.items():
        score = len(active & expected) * 2 - len(active - expected) - len(expected - active)
        if score > best_score:
            best_digit = digit
            best_score = score
    return best_digit if best_score >= 2 else None
