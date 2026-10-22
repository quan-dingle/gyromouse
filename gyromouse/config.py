from __future__ import annotations

import json
from pathlib import Path
from copy import deepcopy

APP_DIR = Path.home() / ".gyromouse"
SETTINGS_FILE = APP_DIR / "settings.json"

DEFAULTS = {
    "phone_url": "",
    "capture_mode": "drain",
    "poll_hz": 180,
    "source_group": "auto",
    "x_buffer": "",
    "y_buffer": "",
    "z_buffer": "",
    "time_buffer": "",
    "signal_kind": "linear_acceleration",
    "filter": "one_euro",
    "min_cutoff": 2.4,
    "beta": 0.20,
    "derivative_cutoff": 1.0,
    "ema_cutoff": 12.0,
    "motion_mode": "integrate",
    "gain": 30000.0,
    "direct_gain": 520.0,
    "friction": 2.5,
    "max_speed": 1800.0,
    "deadzone": 0.035,
    "deadzone_enabled": True,
    "response_curve": 1.0,
    "axis_x": "x",
    "axis_y": "y",
    "invert_x": False,
    "invert_y": True,
    "adaptive_bias": True,
    "auto_calibrate_on_start": True,
    "stationary_threshold": 0.17,
    "stationary_reset_ms": 140,
    "mouse_enabled": True,
    "click_enabled": False,
    "click_z_threshold": 3.0,
    "click_xy_sum": 20.0,
    "click_cooldown_ms": 350,
    "start_measurement": True,
}


def clean_settings(settings: dict | None) -> dict:
    result = deepcopy(DEFAULTS)
    if settings:
        for key in DEFAULTS:
            if key in settings:
                result[key] = settings[key]
    numeric = {
        "poll_hz": (30, 300),
        "min_cutoff": (0.2, 12.0),
        "beta": (0.0, 1.5),
        "derivative_cutoff": (0.2, 10.0),
        "ema_cutoff": (0.2, 60.0),
        "gain": (100.0, 100000.0),
        "direct_gain": (10.0, 3000.0),
        "friction": (0.0, 18.0),
        "max_speed": (100.0, 6000.0),
        "deadzone": (0.0, 3.0),
        "response_curve": (0.5, 2.0),
        "stationary_threshold": (0.01, 2.0),
        "stationary_reset_ms": (20, 1000),
        "click_z_threshold": (0.1, 20.0),
        "click_xy_sum": (0.1, 300.0),
        "click_cooldown_ms": (100, 2000),
    }
    for key, (low, high) in numeric.items():
        try:
            value = float(result[key])
        except (TypeError, ValueError):
            value = float(DEFAULTS[key])
        value = min(high, max(low, value))
        if key in {"poll_hz", "stationary_reset_ms", "click_cooldown_ms"}:
            value = int(value)
        result[key] = value
    for key in ("deadzone_enabled", "invert_x", "invert_y", "adaptive_bias", "auto_calibrate_on_start", "mouse_enabled", "click_enabled", "start_measurement"):
        result[key] = bool(result[key])
    if result["capture_mode"] not in {"drain", "latest"}:
        result["capture_mode"] = "drain"
    if result["motion_mode"] not in {"integrate", "direct"}:
        result["motion_mode"] = "integrate"
    if result["filter"] not in {"raw", "ema", "one_euro", "median_one_euro"}:
        result["filter"] = "one_euro"
    for key in ("axis_x", "axis_y"):
        if result[key] not in {"x", "y", "z"}:
            result[key] = "x" if key == "axis_x" else "y"
    result["phone_url"] = str(result["phone_url"]).strip().rstrip("/")
    for key in ("x_buffer", "y_buffer", "z_buffer", "time_buffer", "source_group", "signal_kind"):
        result[key] = str(result[key] or "").strip()
    return result


def load_settings() -> dict:
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    return clean_settings(data)


def save_settings(settings: dict) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    temp = SETTINGS_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(clean_settings(settings), indent=2), encoding="utf-8")
    temp.replace(SETTINGS_FILE)
