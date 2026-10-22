from __future__ import annotations

from threading import Lock
import time

from .config import DEFAULTS, clean_settings, load_settings, save_settings
from .engine import MotionEngine
from .filters import filter_catalog
from .phyphox import PhyphoxClient, normalize_url


class DesktopAPI:
    def __init__(self):
        self._lock = Lock()
        self.settings = load_settings()
        self.engine = MotionEngine()
        self.engine.update_settings(self.settings)
        self.client: PhyphoxClient | None = None
        self.discovery: dict = {"title": "Not connected", "buffers": [], "groups": [], "meta": {}, "rtt_ms": 0.0}

    def bootstrap(self) -> dict:
        result = self.engine.snapshot()
        result.update({
            "settings": self.settings.copy(),
            "filters": filter_catalog(),
            "discovery": self.discovery.copy(),
            "defaults": DEFAULTS.copy(),
        })
        return result

    def connect(self, url: str = "") -> dict:
        try:
            with self._lock:
                if self.engine.snapshot()["running"]:
                    self.engine.stop()
                requested = normalize_url(url or self.settings.get("phone_url", ""))
                client = PhyphoxClient(requested)
                discovery = client.discover()
                preferred = self.settings.get("source_group", "auto")
                group = next((g for g in discovery["groups"] if g["key"] == preferred), None)
                if group is None:
                    group = next((g for g in discovery["groups"] if g["kind"] == "accelerometer"), None)
                if group is None:
                    group = next((g for g in discovery["groups"] if g["kind"] == "linear_acceleration"), None)
                if group is None and discovery["groups"]:
                    group = discovery["groups"][0]
                self.settings["phone_url"] = requested
                if group:
                    self.settings["source_group"] = group["key"]
                    self.settings.update(group["mapping"])
                    self.settings["signal_kind"] = group["kind"]
                else:
                    self.settings["source_group"] = "auto"
                    for key in ("x_buffer", "y_buffer", "z_buffer", "time_buffer"):
                        if self.settings.get(key) not in {item["name"] for item in discovery["buffers"]}:
                            self.settings[key] = ""
                self.client = client
                self.discovery = discovery
                self.engine.set_client(client)
                self.engine.update_settings(self.settings)
                save_settings(self.settings)
                message = f"Connected to {discovery['title']}." if group else f"Connected to {discovery['title']}; map X/Y/Z manually."
                return {"ok": True, "message": message, "state": self.bootstrap()}
        except Exception as exc:
            return {"ok": False, "message": str(exc)}

    def update_settings(self, patch: dict) -> dict:
        try:
            if not isinstance(patch, dict):
                return {"ok": False, "message": "Settings payload must be an object."}
            with self._lock:
                merged = self.settings.copy()
                merged.update(patch)
                group_key = merged.get("source_group", "")
                if group_key != self.settings.get("source_group") and self.discovery.get("groups"):
                    group = next((g for g in self.discovery["groups"] if g["key"] == group_key), None)
                    if group:
                        merged.update(group["mapping"])
                        merged["signal_kind"] = group["kind"]
                self.settings = clean_settings(merged)
                self.engine.update_settings(self.settings)
                save_settings(self.settings)
            return {"ok": True, "settings": self.settings.copy()}
        except Exception as exc:
            return {"ok": False, "message": str(exc)}

    def start_tracking(self) -> dict:
        if self.client is None:
            result = self.connect(self.settings.get("phone_url", ""))
            if not result.get("ok"):
                return result
        try:
            probe = self.client.latest(self.settings)
            if not probe.get("status", {}).get("measuring", False):
                if self.settings.get("start_measurement", True):
                    if not self.client.control("start"):
                        return {"ok": False, "message": "phyphox did not accept the start command. Start the experiment on your phone."}
                    time.sleep(0.10)
                else:
                    return {"ok": False, "message": "The phyphox experiment is paused. Start it on the phone or enable auto-start."}
            result = self.engine.start()
            if result.get("ok"):
                return {"ok": True, "message": "Tracking started. Mouse movement is live.", "state": self.engine.snapshot()}
            return result
        except Exception as exc:
            return {"ok": False, "message": str(exc)}

    def stop_tracking(self) -> dict:
        return self.engine.stop()

    def calibrate(self) -> dict:
        return self.engine.calibrate()

    def reset_velocity(self) -> dict:
        return self.engine.reset_motion()

    def get_state(self) -> dict:
        result = self.engine.snapshot()
        result.update({
            "settings": self.settings.copy(),
            "filters": filter_catalog(),
            "discovery": self.discovery.copy(),
        })
        return result

    def shutdown(self) -> None:
        self.engine.stop()
