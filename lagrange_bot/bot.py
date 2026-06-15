from __future__ import annotations

import argparse
import json
import time
from datetime import datetime
from pathlib import Path

from .capture_backends import capture_window_client, stop_wgc_sessions
from .config import BotConfig
from .decision import DeckPolicy
from .executor import ActionExecutor
from .local_assets import DEFAULT_KEYWORDS, DEFAULT_PATH_FILTERS, inspect_local_game
from .models import Action, ActionType, GameState, Phase, Point, SlotConfig, VisibleCard
from .ship_catalog import build_ship_catalog, write_ship_catalog_report
from .vision import ScreenReader, card_title_rect_for_slot, crop_image, normalize_capture_image, slot_looks_playable
from .windowing import find_window, focus_window, get_window, set_window_topmost


def _load_image(path: str):
    try:
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError("Pillow is required for --image") from exc
    return Image.open(path).convert("RGB")


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _safe_file_stem(text: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in text.strip())
    safe = safe.strip("_")
    return safe[:64] or "item"


def _state_to_dict(state):
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


def _offset_point(point: Point | None, origin: Point, scale: tuple[float, float]) -> Point | None:
    if point is None:
        return None
    return int(round(point[0] * scale[0])) + origin[0], int(round(point[1] * scale[1])) + origin[1]


def _offset_action(action: Action, origin: Point, scale: tuple[float, float]) -> Action:
    if origin == (0, 0) and scale == (1.0, 1.0):
        return action
    return Action(
        type=action.type,
        reason=action.reason,
        pre_clicks=tuple(_offset_point(point, origin, scale) for point in action.pre_clicks),  # type: ignore[arg-type]
        click=_offset_point(action.click, origin, scale),
        card_id=action.card_id,
        skill_id=action.skill_id,
        target_click=_offset_point(action.target_click, origin, scale),
        wait_seconds=action.wait_seconds,
    )


def _offset_rect(rect: tuple[int, int, int, int], dx: int = 0, dy: int = 0) -> tuple[int, int, int, int]:
    return rect[0] + dx, rect[1] + dy, rect[2], rect[3]


def _hand_slots_for_state(config: BotConfig, state: GameState) -> list[SlotConfig]:
    if not state.layout_offset_x and not state.layout_offset_y:
        return config.hand_slots
    return [
        SlotConfig(
            name=slot.name,
            rect=_offset_rect(slot.rect, state.layout_offset_x, state.layout_offset_y),
            click=(slot.click[0] + state.layout_offset_x, slot.click[1] + state.layout_offset_y),
        )
        for slot in config.hand_slots
    ]


def cmd_detect(args) -> int:
    config = BotConfig.load(args.config)
    reader = ScreenReader(config)
    phase_override = Phase(args.phase) if getattr(args, "phase", None) else None
    if args.image:
        image = _load_image(args.image)
        state = reader.read_state_from_image(image, now_seconds=args.time, phase_override=phase_override)
    else:
        image = reader.capture()
        state = reader.read_state_from_image(
            image,
            now_seconds=args.time,
            phase_override=phase_override,
            capture_origin=reader.last_capture_origin,
            capture_scale=reader.last_capture_scale,
        )
    print(json.dumps(_state_to_dict(state), ensure_ascii=False, indent=2))
    policy = DeckPolicy(config)
    action = policy.decide(state)
    print(
        json.dumps(
            {
                "action": action.type.value,
                "reason": action.reason,
                "pre_clicks": list(action.pre_clicks),
                "click": action.click,
                "target_click": action.target_click,
                "card_id": action.card_id,
                "skill_id": action.skill_id,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _action_to_dict(action: Action) -> dict[str, object]:
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


def cmd_crop_regions(args) -> int:
    config = BotConfig.load(args.config)
    image = _load_image(args.image)
    output_dir = Path(args.output).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    regions: list[tuple[str, tuple[int, int, int, int]]] = []
    regions.extend((slot.name, slot.rect) for slot in config.hand_slots)
    if config.reserve_slot:
        regions.append((config.reserve_slot.name, config.reserve_slot.rect))
    cost_rect = config.cost.get("rect")
    if cost_rect:
        regions.append(("cost", tuple(int(v) for v in cost_rect)))
    for skill in config.skills:
        if skill.rect:
            regions.append((f"skill_{skill.id}", skill.rect))
    for item in config.data.get("calibration_regions", []):
        regions.append((str(item["name"]), tuple(int(v) for v in item["rect"])))

    manifest = []
    for name, rect in regions:
        crop = crop_image(image, rect)
        safe_name = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in name)
        path = output_dir / f"{safe_name}.png"
        crop.save(path)
        manifest.append({"name": name, "rect": rect, "path": str(path)})

    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output_dir": str(output_dir), "count": len(manifest)}, ensure_ascii=False, indent=2))
    return 0


def _relative_or_absolute(root: Path, path_text: str | None) -> Path | None:
    if not path_text:
        return None
    path = Path(path_text)
    if path.is_absolute():
        return path
    return root / path


def cmd_validate_assets(args) -> int:
    config = BotConfig.load(args.config)
    checks = []

    for card in config.cards.values():
        checks.append(("card", card.id, card.name, Path(card.template)))

    for skill in config.skills:
        if skill.template:
            checks.append(("skill", skill.id, skill.name, Path(skill.template)))

    for item in config.data.get("battlefield", {}).get("targets", []):
        path = _relative_or_absolute(config.root, item.get("template"))
        checks.append(("battlefield", str(item["id"]), str(item.get("name", item["id"])), path))

    results = []
    missing = 0
    for kind, item_id, name, path in checks:
        exists = bool(path and path.exists())
        if not exists:
            missing += 1
        results.append(
            {
                "kind": kind,
                "id": item_id,
                "name": name,
                "exists": exists,
                "path": str(path) if path else None,
            }
        )

    print(json.dumps({"missing": missing, "assets": results}, ensure_ascii=False, indent=2))
    return 1 if missing and args.strict else 0


def cmd_run(args) -> int:
    config = BotConfig.load(args.config)
    dry_run = config.dry_run_default
    if args.dry_run:
        dry_run = True
    if args.live:
        dry_run = False

    reader = ScreenReader(config)
    policy = DeckPolicy(config)
    executor = ActionExecutor(
        dry_run=dry_run,
        click_delay_seconds=config.global_click_delay_seconds,
    )
    played_counts: dict[str, int] = {}

    print(f"profile={config.profile_name} dry_run={dry_run}")
    while True:
        state = reader.read_state()
        state.played_counts = dict(played_counts)
        action = policy.decide(state)
        execute_action = (
            _offset_action(action, state.capture_origin, state.capture_scale)
            if not dry_run
            else action
        )
        executor.execute(execute_action)
        if action.type == ActionType.CAST_SKILL:
            reader.mark_action_executed(action.type.value, action.skill_id, state.now_seconds)
        if action.type == ActionType.PLAY_CARD and not dry_run and action.card_id:
            played_counts[action.card_id] = played_counts.get(action.card_id, 0) + 1
        if args.once:
            break
        time.sleep(config.loop_interval_seconds)
    return 0


def cmd_collect_training(args) -> int:
    config = BotConfig.load(args.config)
    if args.hwnd:
        window = get_window(int(args.hwnd))
    else:
        title = args.title or str(config.screen.get("window_title_contains", ""))
        window = find_window(title)
    topmost_set = False
    try:
        focus_window(window.hwnd)
        set_window_topmost(window.hwnd, True)
        topmost_set = True
    except Exception as exc:
        print(
            json.dumps(
                {
                    "event": "training_window_topmost_error",
                    "hwnd": window.hwnd,
                    "title": window.title,
                    "error": str(exc),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

    started_at = datetime.now().astimezone()
    output_dir = Path(args.output or Path("training_samples") / f"hand_live_{started_at.strftime('%Y%m%d_%H%M%S_%f')}").resolve()
    for child in ("raw", "slots", "titles", "timer", "command"):
        (output_dir / child).mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "manifest.jsonl"
    reader = ScreenReader(config)
    interval_seconds = max(0.0, float(args.interval_ms) / 1000.0)
    end_monotonic = time.monotonic() + max(0.0, float(args.duration)) if args.duration else None
    frame_index = 0

    print(
        json.dumps(
            {
                "event": "training_collection_start",
                "dir": str(output_dir),
                "manifest": str(manifest_path),
                "hwnd": window.hwnd,
                "title": window.title,
                "interval_ms": args.interval_ms,
                "duration": args.duration,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    try:
        while True:
            loop_started = time.perf_counter()
            window = get_window(window.hwnd)
            image, origin, monitor = capture_window_client(window, config)
            capture_ms = round((time.perf_counter() - loop_started) * 1000, 1)
            image, scale = normalize_capture_image(image, config)
            state = reader.read_state_from_image(
                image,
                now_seconds=args.time,
                capture_origin=origin,
                capture_scale=scale,
            )
            frame_index += 1
            stem = f"{frame_index:05d}"
            raw_path = output_dir / "raw" / f"{stem}_raw.png"
            image.save(raw_path)
            print(
                json.dumps(
                    {
                        "event": "window_captured",
                        "frame_index": frame_index,
                        "hwnd": window.hwnd,
                        "title": window.title,
                        "client_capture_rect": window.client_capture_rect,
                        "capture_origin": origin,
                        "image_size": image.size,
                        "capture_ms": capture_ms,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            hand_slots = _hand_slots_for_state(config, state)
            slots: list[dict[str, object]] = []
            for slot in hand_slots:
                slot_path = output_dir / "slots" / f"{stem}_{_safe_file_stem(slot.name)}.png"
                title_path = output_dir / "titles" / f"{stem}_{_safe_file_stem(slot.name)}_title.png"
                crop_image(image, slot.rect).save(slot_path)
                title_rect = card_title_rect_for_slot(config, slot.rect)
                crop_image(image, title_rect).save(title_path)
                slots.append(
                    {
                        "slot": slot.name,
                        "slot_rect": slot.rect,
                        "title_rect": title_rect,
                        "playable_guess": slot_looks_playable(config, image, slot.rect),
                        "slot_image": str(slot_path),
                        "title_image": str(title_path),
                    }
                )

            timer_paths: list[dict[str, object]] = []
            for index, rect in enumerate(reader._timer_rects_for_offset(0)):
                path = output_dir / "timer" / f"{stem}_timer_{index}.png"
                crop_image(image, rect).save(path)
                timer_paths.append({"rect": rect, "image": str(path)})

            command_paths: list[dict[str, object]] = []
            for name, rect in reader._cost_rects_for_offset(0):
                path = output_dir / "command" / f"{stem}_command_{name}.png"
                crop_image(image, rect).save(path)
                command_paths.append({"name": name, "rect": rect, "image": str(path)})

            record = {
                "time": _now_iso(),
                "frame_index": frame_index,
                "source": {
                    "hwnd": window.hwnd,
                    "title": window.title,
                    "client_capture_rect": window.client_capture_rect,
                    "capture_monitor": monitor,
                    "capture_origin": origin,
                    "capture_scale": scale,
                },
                "screen_timer_seconds": reader.last_match_timer_seconds,
                "raw_timer_seconds": reader.last_raw_timer_seconds,
                "layout_offset_y": state.layout_offset_y,
                "read_state_timings": reader.last_read_state_timings,
                "hand_card_diagnostics": reader.last_hand_card_diagnostics,
                "raw_image": str(raw_path),
                "slots": slots,
                "timer": timer_paths,
                "command": command_paths,
                "label_fields": {
                    "true_cards_by_slot": {},
                    "true_timer_seconds": None,
                    "true_command_value": None,
                },
            }
            with manifest_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
            print(
                json.dumps(
                    {
                        "event": "training_frame_saved",
                        "frame_index": frame_index,
                        "elapsed_ms": round((time.perf_counter() - loop_started) * 1000, 1),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

            if args.frames and frame_index >= int(args.frames):
                break
            if end_monotonic is not None and time.monotonic() >= end_monotonic:
                break
            sleep_seconds = interval_seconds - (time.perf_counter() - loop_started)
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)
    finally:
        stop_wgc_sessions()
        if topmost_set:
            try:
                set_window_topmost(window.hwnd, False)
            except Exception:
                pass

    print(
        json.dumps(
            {
                "event": "training_collection_stop",
                "frames": frame_index,
                "dir": str(output_dir),
                "manifest": str(manifest_path),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 0


def _normalize_battle_zoom(
    window,
    seconds: float,
    scroll_amount: int,
    interval_seconds: float,
) -> None:
    duration = max(0.0, float(seconds))
    if duration <= 0:
        return
    try:
        import pyautogui
    except ImportError:
        print(
            json.dumps(
                {
                    "event": "battle_zoom_normalize_skipped",
                    "reason": "pyautogui_missing",
                    "seconds": duration,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return

    focus_window(window.hwnd)
    left, top, width, height = window.client_capture_rect
    center = (left + width // 2, top + height // 2)
    pyautogui.moveTo(center[0], center[1])
    deadline = time.monotonic() + duration
    steps = 0
    print(
        json.dumps(
            {
                "event": "battle_zoom_normalize_start",
                "seconds": duration,
                "scroll_amount": scroll_amount,
                "interval_seconds": interval_seconds,
                "center": center,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    while time.monotonic() < deadline:
        pyautogui.scroll(int(scroll_amount))
        steps += 1
        time.sleep(max(0.01, float(interval_seconds)))
    print(
        json.dumps(
            {
                "event": "battle_zoom_normalize_done",
                "steps": steps,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )


def cmd_collect_battle(args) -> int:
    config = BotConfig.load(args.config)
    if args.hwnd:
        window = get_window(int(args.hwnd))
    else:
        title = args.title or str(config.screen.get("window_title_contains", ""))
        window = find_window(title)

    topmost_set = False
    try:
        focus_window(window.hwnd)
        set_window_topmost(window.hwnd, True)
        topmost_set = True
    except Exception as exc:
        print(
            json.dumps(
                {
                    "event": "battle_collection_window_topmost_error",
                    "hwnd": window.hwnd,
                    "title": window.title,
                    "error": str(exc),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

    if args.normalize_zoom_seconds:
        _normalize_battle_zoom(
            window,
            seconds=float(args.normalize_zoom_seconds),
            scroll_amount=int(args.normalize_zoom_scroll),
            interval_seconds=float(args.normalize_zoom_interval_ms) / 1000.0,
        )

    started_at = datetime.now().astimezone()
    output_dir = Path(args.output or Path("training_samples") / f"battle_live_{started_at.strftime('%Y%m%d_%H%M%S_%f')}").resolve()
    for child in ("raw", "skills", "battlefield", "timer"):
        (output_dir / child).mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "manifest.jsonl"
    reader = ScreenReader(config)
    policy = DeckPolicy(config)
    interval_seconds = max(0.0, float(args.interval_ms) / 1000.0)
    end_monotonic = time.monotonic() + max(0.0, float(args.duration)) if args.duration else None
    frame_index = 0

    print(
        json.dumps(
            {
                "event": "battle_collection_start",
                "dir": str(output_dir),
                "manifest": str(manifest_path),
                "hwnd": window.hwnd,
                "title": window.title,
                "interval_ms": args.interval_ms,
                "duration": args.duration,
                "normalize_zoom_seconds": args.normalize_zoom_seconds,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    try:
        while True:
            loop_started = time.perf_counter()
            window = get_window(window.hwnd)
            image, origin, monitor = capture_window_client(window, config)
            capture_ms = round((time.perf_counter() - loop_started) * 1000, 1)
            image, scale = normalize_capture_image(image, config)
            state = reader.read_state_from_image(
                image,
                now_seconds=args.time,
                phase_override=Phase.BATTLE,
                capture_origin=origin,
                capture_scale=scale,
            )
            action = policy.decide(state)
            battle_valid_after_seconds = float(config.phase.get("battle_valid_after_seconds", 130.0))
            battle_valid = state.now_seconds >= battle_valid_after_seconds
            frame_index += 1
            stem = f"{frame_index:05d}"
            raw_path = output_dir / "raw" / f"{stem}_raw.png"
            image.save(raw_path)

            skill_crops: list[dict[str, object]] = []
            for skill in config.skills:
                if not skill.active or not skill.rect:
                    continue
                path = output_dir / "skills" / f"{stem}_{_safe_file_stem(skill.id)}.png"
                crop_image(image, skill.rect).save(path)
                skill_crops.append(
                    {
                        "skill_id": skill.id,
                        "name": skill.name,
                        "rect": skill.rect,
                        "click": skill.click,
                        "image": str(path),
                    }
                )

            battlefield_crops: list[dict[str, object]] = []
            for item in config.data.get("battlefield", {}).get("targets", []):
                rect = tuple(int(v) for v in item.get("search_rect", [0, 0, 0, 0]))
                if rect[2] <= 0 or rect[3] <= 0:
                    continue
                path = output_dir / "battlefield" / f"{stem}_{_safe_file_stem(str(item['id']))}_search.png"
                crop_image(image, rect).save(path)
                battlefield_crops.append(
                    {
                        "target_id": str(item["id"]),
                        "name": str(item.get("name", item["id"])),
                        "search_rect": rect,
                        "image": str(path),
                    }
                )

            timer_paths: list[dict[str, object]] = []
            for index, rect in enumerate(reader._timer_rects_for_offset(0)):
                path = output_dir / "timer" / f"{stem}_timer_{index}.png"
                crop_image(image, rect).save(path)
                timer_paths.append({"rect": rect, "image": str(path)})

            record = {
                "time": _now_iso(),
                "frame_index": frame_index,
                "source": {
                    "hwnd": window.hwnd,
                    "title": window.title,
                    "client_capture_rect": window.client_capture_rect,
                    "capture_monitor": monitor,
                    "capture_origin": origin,
                    "capture_scale": scale,
                },
                "screen_timer_seconds": reader.last_match_timer_seconds,
                "raw_timer_seconds": reader.last_raw_timer_seconds,
                "read_state_timings": reader.last_read_state_timings,
                "timer_attempts": reader.last_timer_attempts,
                "timer_stabilizer": reader.last_timer_stabilizer,
                "battlefield_diagnostics": reader.last_battlefield_diagnostics,
                "raw_image": str(raw_path),
                "skills": skill_crops,
                "battlefield": battlefield_crops,
                "timer": timer_paths,
                "state": _state_to_dict(state),
                "action": _action_to_dict(action),
                "screen_action": _action_to_dict(_offset_action(action, state.capture_origin, state.capture_scale)),
                "battle_valid": battle_valid,
                "battle_valid_after_seconds": battle_valid_after_seconds,
                "label_fields": {
                    "true_active_skills": [],
                    "true_cas066_targets": [],
                    "notes": "",
                },
            }
            with manifest_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
            print(
                json.dumps(
                    {
                        "event": "battle_frame_saved",
                        "frame_index": frame_index,
                        "battle_valid": battle_valid,
                        "time": round(state.now_seconds, 2),
                        "targets": len(state.battlefield_targets),
                        "action": action.type.value,
                        "skill_id": action.skill_id,
                        "target_click": action.target_click,
                        "elapsed_ms": round((time.perf_counter() - loop_started) * 1000, 1),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

            if args.frames and frame_index >= int(args.frames):
                break
            if end_monotonic is not None and time.monotonic() >= end_monotonic:
                break
            sleep_seconds = interval_seconds - (time.perf_counter() - loop_started)
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)
    finally:
        stop_wgc_sessions()
        if topmost_set:
            try:
                set_window_topmost(window.hwnd, False)
            except Exception:
                pass

    print(
        json.dumps(
            {
                "event": "battle_collection_stop",
                "frames": frame_index,
                "dir": str(output_dir),
                "manifest": str(manifest_path),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 0


def cmd_simulate(args) -> int:
    config = BotConfig.load(args.config)
    visible_cards: list[VisibleCard] = []
    for index, card_id in enumerate(args.hand):
        if card_id not in config.cards:
            raise SystemExit(f"unknown card id: {card_id}")
        if index < len(config.hand_slots):
            slot = config.hand_slots[index]
        else:
            slot = SlotConfig(f"sim_{index + 1}", (0, 0, 1, 1), (0, 0))
        visible_cards.append(VisibleCard(config.cards[card_id], slot, 1.0))

    phase = Phase(args.phase)
    played_counts = {}
    for item in args.played:
        card_id, _, count_text = item.partition(":")
        if not card_id:
            continue
        played_counts[card_id] = int(count_text or "1")

    state = GameState(
        now_seconds=args.time,
        phase=phase,
        cost=args.cost,
        visible_cards=visible_cards,
        reserve_card_id=args.reserve,
        played_counts=played_counts,
        skills=[],
        hand_slot_playable={slot.name: True for slot in config.hand_slots},
    )
    action = DeckPolicy(config).decide(state)
    print(
        json.dumps(
            {
                "state": _state_to_dict(state),
                "action": {
                    "type": action.type.value,
                    "reason": action.reason,
                    "pre_clicks": list(action.pre_clicks),
                    "click": action.click,
                    "target_click": action.target_click,
                    "card_id": action.card_id,
                    "skill_id": action.skill_id,
                    "wait_seconds": action.wait_seconds,
                },
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_print_config_path(args) -> int:
    print(Path(args.config).resolve())
    return 0


def cmd_inspect_local(args) -> int:
    report = inspect_local_game(
        explicit_path=args.game_root,
        keywords=args.keyword or DEFAULT_KEYWORDS,
        path_filters=args.path_filter or DEFAULT_PATH_FILTERS,
        max_paths_per_package=args.max_paths,
        max_keyword_hits=args.max_keyword_hits,
        scan_keywords=args.scan_keywords,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("found") else 1


def cmd_ship_catalog(args) -> int:
    catalog = build_ship_catalog(
        game_root=args.game_root,
        max_examples_per_kind=args.max_examples,
    )
    if args.output:
        output = Path(args.output).resolve()
        write_ship_catalog_report(catalog, output)
        print(
            json.dumps(
                {
                    "found": catalog.get("found"),
                    "ships": len(catalog.get("ships", [])),
                    "report": str(output),
                    "json": str(output.with_suffix(".json")),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(json.dumps(catalog, ensure_ascii=False, indent=2))
    return 0 if catalog.get("found") else 1


def cmd_gui(args) -> int:
    from .gui import launch_gui

    launch_gui(args.config)
    return 0


def cmd_data_gui(args) -> int:
    from .data_gui import launch_data_gui

    launch_data_gui(args.config)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Authorized private-server Lagrange automation scaffold.")
    sub = parser.add_subparsers(dest="command", required=True)

    detect = sub.add_parser("detect", help="Capture one frame, identify state, and print the suggested action.")
    detect.add_argument("--config", required=True)
    detect.add_argument("--image", default=None, help="Read a screenshot file instead of capturing the screen.")
    detect.add_argument("--time", type=float, default=None, help="Override match time in seconds for --image.")
    detect.add_argument(
        "--phase",
        choices=[Phase.PLACEMENT.value, Phase.EXTRA_PLACE.value, Phase.BATTLE.value],
        default=None,
        help="Override phase before reading phase-specific regions.",
    )
    detect.set_defaults(func=cmd_detect)

    crop_regions = sub.add_parser("crop-regions", help="Crop configured regions from a screenshot for calibration.")
    crop_regions.add_argument("--config", required=True)
    crop_regions.add_argument("--image", required=True)
    crop_regions.add_argument("--output", required=True)
    crop_regions.set_defaults(func=cmd_crop_regions)

    validate_assets = sub.add_parser("validate-assets", help="Check whether configured template image files exist.")
    validate_assets.add_argument("--config", required=True)
    validate_assets.add_argument("--strict", action="store_true", help="Exit with code 1 when any asset is missing.")
    validate_assets.set_defaults(func=cmd_validate_assets)

    run = sub.add_parser("run", help="Run the detect-decide-execute loop.")
    run.add_argument("--config", required=True)
    run.add_argument("--dry-run", action="store_true", help="Print actions without clicking.")
    run.add_argument("--live", action="store_true", help="Enable real mouse clicks.")
    run.add_argument("--once", action="store_true", help="Run one loop iteration.")
    run.set_defaults(func=cmd_run)

    collect_training = sub.add_parser(
        "collect-training",
        help="Collect hand/timer/command training crops in a separate capture loop.",
    )
    collect_training.add_argument("--config", required=True)
    collect_training.add_argument("--hwnd", type=int, default=None, help="Window handle to capture.")
    collect_training.add_argument("--title", default=None, help="Window title text when --hwnd is omitted.")
    collect_training.add_argument("--output", default=None, help="Training sample output directory.")
    collect_training.add_argument("--interval-ms", type=int, default=1000)
    collect_training.add_argument("--duration", type=float, default=120.0)
    collect_training.add_argument("--frames", type=int, default=None)
    collect_training.add_argument("--time", type=float, default=None, help="Optional match-time override.")
    collect_training.set_defaults(func=cmd_collect_training)

    collect_battle = sub.add_parser(
        "collect-battle",
        help="Collect battle skill slots, CAS066 battlefield labels, and simulated skill actions.",
    )
    collect_battle.add_argument("--config", required=True)
    collect_battle.add_argument("--hwnd", type=int, default=None, help="Window handle to capture.")
    collect_battle.add_argument("--title", default=None, help="Window title text when --hwnd is omitted.")
    collect_battle.add_argument("--output", default=None, help="Battle sample output directory.")
    collect_battle.add_argument("--interval-ms", type=int, default=750)
    collect_battle.add_argument("--duration", type=float, default=120.0)
    collect_battle.add_argument("--frames", type=int, default=None)
    collect_battle.add_argument("--time", type=float, default=140.0, help="Optional battle-time override.")
    collect_battle.add_argument("--normalize-zoom-seconds", type=float, default=0.0)
    collect_battle.add_argument("--normalize-zoom-scroll", type=int, default=-5)
    collect_battle.add_argument("--normalize-zoom-interval-ms", type=int, default=100)
    collect_battle.set_defaults(func=cmd_collect_battle)

    simulate = sub.add_parser("simulate", help="Evaluate deck policy from text inputs, without screen capture.")
    simulate.add_argument("--config", required=True)
    simulate.add_argument("--cost", type=int, required=True)
    simulate.add_argument("--time", type=float, default=0)
    simulate.add_argument(
        "--phase",
        choices=[Phase.PLACEMENT.value, Phase.EXTRA_PLACE.value, Phase.BATTLE.value],
        default=Phase.PLACEMENT.value,
    )
    simulate.add_argument("--hand", nargs="+", required=True, help="Visible card ids in hand-slot order.")
    simulate.add_argument("--reserve", default=None, help="Optional reserve card id.")
    simulate.add_argument(
        "--played",
        nargs="*",
        default=[],
        help="Played counts like card_id:2. Omit count for 1.",
    )
    simulate.set_defaults(func=cmd_simulate)

    path_cmd = sub.add_parser("config-path", help="Print the absolute config path.")
    path_cmd.add_argument("--config", required=True)
    path_cmd.set_defaults(func=cmd_print_config_path)

    inspect_local = sub.add_parser(
        "inspect-local",
        help="Read-only scan of a local Lagrange install and NXPK package path tables.",
    )
    inspect_local.add_argument("--game-root", default=None, help="Optional game root. Auto-detects by default.")
    inspect_local.add_argument(
        "--path-filter",
        action="append",
        default=None,
        help="Only show resource paths containing this text. Can be repeated.",
    )
    inspect_local.add_argument(
        "--keyword",
        action="append",
        default=None,
        help="Chinese/ASCII text to search inside packages when --scan-keywords is set. Can be repeated.",
    )
    inspect_local.add_argument("--max-paths", type=int, default=80, help="Maximum resource path hits per package.")
    inspect_local.add_argument("--max-keyword-hits", type=int, default=8, help="Maximum text hits per keyword.")
    inspect_local.add_argument(
        "--scan-keywords",
        action="store_true",
        help="Also scan full packages for card/skill name text. This is slower.",
    )
    inspect_local.set_defaults(func=cmd_inspect_local)

    ship_catalog = sub.add_parser(
        "ship-catalog",
        help="Build a read-only ship resource catalog from local NXPK indexes.",
    )
    ship_catalog.add_argument("--game-root", default=None, help="Optional game root. Auto-detects by default.")
    ship_catalog.add_argument("--output", default=None, help="Write a Markdown report and sibling JSON file.")
    ship_catalog.add_argument("--max-examples", type=int, default=10, help="Maximum sample paths per resource kind.")
    ship_catalog.set_defaults(func=cmd_ship_catalog)

    gui = sub.add_parser("gui", help="Open the local GUI test panel.")
    gui.add_argument("--config", default=str(Path("configs") / "star_hunter_1920.json"))
    gui.set_defaults(func=cmd_gui)

    data_gui = sub.add_parser("data-gui", help="Open the data collection GUI.")
    data_gui.add_argument("--config", default=str(Path("configs") / "star_hunter_1920.json"))
    data_gui.set_defaults(func=cmd_data_gui)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if getattr(args, "dry_run", False) and getattr(args, "live", False):
        parser.error("--dry-run and --live cannot be used together")
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
