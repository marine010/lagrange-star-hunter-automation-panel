from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import BotConfig


CALIBRATION_VERSION = 1


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _safe_name(value: str) -> str:
    chars: list[str] = []
    for char in value:
        if char.isalnum() or char in {"-", "_", "."}:
            chars.append(char)
        else:
            chars.append("_")
    return "".join(chars).strip("_") or "default"


def _config_digest(config: BotConfig) -> str | None:
    try:
        return hashlib.sha256(config.path.read_bytes()).hexdigest()[:16]
    except OSError:
        return None


def calibration_identity(config: BotConfig) -> dict[str, Any]:
    return {
        "version": CALIBRATION_VERSION,
        "profile_name": config.profile_name,
        "capture_backend": str(config.screen.get("capture_backend", "auto")),
        "reference_size": list(config.screen.get("reference_size", [])),
    }


def calibration_path(config: BotConfig) -> Path:
    profile = _safe_name(config.profile_name)
    backend = _safe_name(str(config.screen.get("capture_backend", "auto")))
    return config.root / "logs" / "local_calibration" / f"{profile}_{backend}.json"


def build_calibration_record(config: BotConfig, calibration: dict[str, Any]) -> dict[str, Any]:
    record = {
        **calibration_identity(config),
        "config_path": str(config.path),
        "config_digest": _config_digest(config),
        "updated_at": _now_iso(),
        "calibration": dict(calibration),
    }
    return record


def save_local_calibration(config: BotConfig, calibration: dict[str, Any]) -> Path:
    path = calibration_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = build_calibration_record(config, calibration)
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_local_calibration(config: BotConfig) -> dict[str, Any] | None:
    path = calibration_path(config)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    identity = calibration_identity(config)
    for key, value in identity.items():
        if record.get(key) != value:
            return None
    calibration = record.get("calibration")
    if not isinstance(calibration, dict):
        return None
    try:
        offset_x = int(calibration.get("offset_x", 0))
        offset_y = int(calibration.get("offset_y", 0))
    except (TypeError, ValueError):
        return None
    return {
        **calibration,
        "offset_x": offset_x,
        "offset_y": offset_y,
        "path": str(path),
        "updated_at": record.get("updated_at"),
    }
