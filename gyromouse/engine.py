from __future__ import annotations

from collections import deque
from statistics import median
from threading import Event, Lock, Thread, current_thread
import math
import time

from .filters import make_filter


class MouseOutput:
    def __init__(self):
        self.controller = None
        self.button = None
        self.error = ""

    def _load(self):
        if self.controller is not None:
            return True
        try:
            from pynput.mouse import Button, Controller
            self.controller = Controller()
            self.button = Button
            self.error = ""
            return True
        except Exception as exc:
            self.error = f"Mouse control unavailable: {exc}"
            return False

    def move(self, x: int, y: int) -> bool:
        if not (x or y) or not self._load():
            return False
        try:
            self.controller.move(int(x), int(y))
            return True
        except Exception as exc:
            self.error = f"Mouse movement failed: {exc}"
            return False

    def click(self) -> bool:
        if not self._load():
            return False
        try:
            self.controller.click(self.button.left, 1)
            return True
        except Exception as exc:
            self.error = f"Mouse click failed: {exc}"
            return False


def _norm(v) -> float:
    return math.sqrt(sum(float(x) * float(x) for x in v))


def _soft_deadzone(value: float, zone: float) -> float:
    magnitude = abs(value)
    if magnitude <= zone:
        return 0.0
    return math.copysign(magnitude - zone, value)


def _shape(value: float, curve: float) -> float:
    if value == 0.0:
        return 0.0
    return math.copysign(abs(value) ** curve, value)


class LegacyClickDetector:
    def __init__(self):
        self.z_history = deque(maxlen=20)
        self.xy_history = deque(maxlen=20)
        self.last_click = 0.0

    def update(self, value: tuple[float, float, float], settings: dict) -> bool:
        x, y, z = value
        self.z_history.append(z)
        self.xy_history.append(math.sqrt(x * x + y * y + z * z))
        if len(self.z_history) < 20:
            return False
        low = sum(1 for v in self.z_history if abs(v) < float(settings["click_z_threshold"]))
        tail = list(self.z_history)[14:]
        recent_not_low = sum(1 for v in tail if abs(v) < 5.0)
        now = time.perf_counter()
        if (low >= 10 and recent_not_low <= 3 and sum(self.xy_history) < float(settings["click_xy_sum"])
                and (now - self.last_click) * 1000 >= float(settings["click_cooldown_ms"])):
            self.last_click = now
            return True
        return False


class MotionEngine:
    def __init__(self):
        self._lock = Lock()
        self._settings = {}
        self._client = None
        self._thread: Thread | None = None
        self._stop = Event()
        self._mouse = MouseOutput()
        self._filter = make_filter("one_euro", {})
        self._last_sample_ts: float | None = None
        self._last_sample_wall: float | None = None
        self._intervals = deque(maxlen=120)
        self._rate_windows = deque(maxlen=240)
        self._bias = [0.0, 0.0, 0.0]
        self._velocity = [0.0, 0.0]
        self._fraction = [0.0, 0.0]
        self._pending_delta = [0.0, 0.0]
        self._filtered = (0.0, 0.0, 0.0)
        self._raw = (0.0, 0.0, 0.0)
        self._calibration = None
        self._calibration_started = 0.0
        self._calibration_samples = []
        self._stationary_since: float | None = None
        self._click_detector = LegacyClickDetector()
        self._stats = self._fresh_stats()
        self._message = "Connect to phyphox to begin."
        self._last_error = ""
        self._request_window = time.perf_counter()
        self._request_count = 0
        self._sample_count = 0
        self._move_count = 0
        self._event_rates = {"poll_hz": 0.0, "sample_hz": 0.0, "move_hz": 0.0}
        self._last_sample_time_series = deque(maxlen=120)
        self._last_output_series = deque(maxlen=120)

    @staticmethod
    def _fresh_stats() -> dict:
        return {
            "polls": 0, "samples": 0, "gap_count": 0, "missing_estimate": 0,
            "buffer_overruns": 0, "errors": 0, "clicks": 0, "rtt_ms": 0.0,
            "sensor_hz": 0.0, "poll_hz": 0.0, "sample_hz": 0.0, "move_hz": 0.0,
            "last_batch": 0, "last_gap_ms": 0.0, "last_sample_age_ms": 0.0,
            "raw": [0.0, 0.0, 0.0], "filtered": [0.0, 0.0, 0.0],
            "velocity": [0.0, 0.0], "bias": [0.0, 0.0, 0.0],
            "calibrating": False, "calibration_samples": 0, "measuring": False,
            "mouse_error": "", "chart": [], "movement_chart": [],
        }

    def set_client(self, client) -> None:
        with self._lock:
            self._client = client
            self._message = "Connected. Review the input mapping, then start tracking."
            self._last_error = ""

    def update_settings(self, settings: dict) -> None:
        with self._lock:
            previous = self._settings.copy()
            self._settings = settings.copy()
            if previous.get("filter") != settings.get("filter"):
                self._filter = make_filter(settings.get("filter", "one_euro"), settings)
                seed = tuple(self._raw[i] - self._bias[i] for i in range(3))
                self._filter.reset(seed)
            else:
                self._filter.settings = settings
            mapping_keys = ("x_buffer", "y_buffer", "z_buffer", "time_buffer", "capture_mode")
            if any(previous.get(k) != settings.get(k) for k in mapping_keys):
                if self._client:
                    self._client.last_timestamp = None
                    self._client._primed = False
                self._last_sample_ts = None
                self._last_sample_wall = None

    def start(self) -> dict:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return {"ok": False, "message": "Tracking is already running."}
            if not self._client:
                return {"ok": False, "message": "Connect to phyphox first."}
            if not all(self._settings.get(k) for k in ("x_buffer", "y_buffer", "z_buffer")):
                return {"ok": False, "message": "Map X, Y and Z buffers first."}
            self._stop.clear()
            self._last_sample_ts = None
            self._last_sample_wall = None
            self._intervals.clear()
            self._rate_windows.clear()
            self._velocity = [0.0, 0.0]
            self._fraction = [0.0, 0.0]
            self._calibration = "collecting" if self._settings.get("auto_calibrate_on_start", True) else None
            self._calibration_started = time.perf_counter()
            self._calibration_samples.clear()
            self._click_detector = LegacyClickDetector()
            self._stats = self._fresh_stats()
            self._stats["calibrating"] = self._calibration == "collecting"
            self._stats["calibration_samples"] = 0
            self._request_window = time.perf_counter()
            self._request_count = self._sample_count = self._move_count = 0
            self._filter = make_filter(self._settings.get("filter", "one_euro"), self._settings)
            self._message = "Tracking started. Keep the phone still while the startup baseline is calibrated." if self._calibration else "Tracking. Keep the phone still and calibrate for best stability."
            self._last_error = ""
            self._client.last_timestamp = None
            self._client._primed = False
            self._thread = Thread(target=self._run, name="Gyromouse acquisition", daemon=True)
            self._thread.start()
            return {"ok": True, "message": self._message}

    def stop(self) -> dict:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive() and thread is not current_thread():
            thread.join(timeout=1.2)
        with self._lock:
            self._thread = None
            self._velocity = [0.0, 0.0]
            self._fraction = [0.0, 0.0]
            self._message = "Tracking stopped."
            self._stats["calibrating"] = False
        return {"ok": True, "message": self._message}

    def reset_motion(self) -> dict:
        with self._lock:
            self._velocity = [0.0, 0.0]
            self._fraction = [0.0, 0.0]
            self._pending_delta = [0.0, 0.0]
            self._stationary_since = None
            seed = tuple(self._raw[i] - self._bias[i] for i in range(3))
            self._filter.reset(seed)
            self._message = "Motion state reset."
        return {"ok": True, "message": self._message}

    def calibrate(self) -> dict:
        with self._lock:
            if not (self._thread and self._thread.is_alive()):
                return {"ok": False, "message": "Start tracking before calibrating."}
            self._calibration = "collecting"
            self._calibration_started = time.perf_counter()
            self._calibration_samples = []
            self._stats["calibrating"] = True
            self._stats["calibration_samples"] = 0
            self._velocity = [0.0, 0.0]
            self._fraction = [0.0, 0.0]
            self._message = "Calibrating. Hold the phone still for about one second."
            return {"ok": True, "message": self._message}

    def _snapshot_settings(self) -> dict:
        with self._lock:
            return self._settings.copy()

    def _run(self) -> None:
        while not self._stop.is_set():
            cycle_start = time.perf_counter()
            settings = self._snapshot_settings()
            client = self._client
            if client is None:
                break
            self._request_count += 1
            try:
                result = client.next_batch(settings, settings.get("capture_mode", "drain"))
                self._last_error = ""
                self._stats["polls"] += 1
                self._stats["rtt_ms"] = result.get("rtt_ms", 0.0)
                status = result.get("status", {})
                self._stats["measuring"] = bool(status.get("measuring", False))
                self._stats["last_batch"] = len(result.get("samples", []))
                if result.get("overflow_suspected"):
                    self._stats["buffer_overruns"] += 1
                if status.get("session_changed"):
                    self._reset_motion()
                    self._message = "phyphox session changed. Re-established the sample baseline."
                if not status.get("measuring", False):
                    self._velocity = [0.0, 0.0]
                    self._fraction = [0.0, 0.0]
                    self._message = "phyphox is paused. Start the measurement on the phone or enable auto-start."
                else:
                    self._pending_delta = [0.0, 0.0]
                    for sample in result.get("samples", []):
                        self._process_sample(sample, settings)
                    self._flush_mouse(settings)
                    if result.get("primed") and not result.get("samples"):
                        self._message = "Sample cursor established. Listening for new sensor readings…"
                self._stats["errors"] = max(0, self._stats["errors"])
            except Exception as exc:
                self._stats["errors"] += 1
                self._last_error = str(exc)
                self._message = f"Input error: {exc}"
                self._stop.wait(0.035)
            self._update_rates(time.perf_counter())
            hz = max(30.0, float(settings.get("poll_hz", 180)))
            remaining = (1.0 / hz) - (time.perf_counter() - cycle_start)
            if remaining > 0:
                self._stop.wait(remaining)

    def _reset_motion(self) -> None:
        self._last_sample_ts = None
        self._last_sample_wall = None
        self._intervals.clear()
        self._velocity = [0.0, 0.0]
        self._fraction = [0.0, 0.0]

    def _process_sample(self, sample, settings: dict) -> None:
        timestamp, x, y, z = (float(v) for v in sample)
        now = time.perf_counter()
        raw = (x, y, z)
        if not all(math.isfinite(v) for v in raw + (timestamp,)):
            return
        if self._last_sample_ts is not None and timestamp <= self._last_sample_ts:
            return

        if self._last_sample_ts is not None:
            dt = timestamp - self._last_sample_ts
            if dt <= 0 or dt > 2.0:
                dt = now - (self._last_sample_wall or now)
        else:
            dt = now - (self._last_sample_wall or now)
        dt = max(0.001, min(0.1, dt))

        if self._last_sample_ts is not None:
            sensor_dt = timestamp - self._last_sample_ts
            expected = median(self._intervals) if self._intervals else None
            is_gap = bool(expected and sensor_dt > max(expected * 1.8, 0.006))
            if is_gap:
                self._stats["gap_count"] += 1
                self._stats["last_gap_ms"] = sensor_dt * 1000.0
                estimated = max(1, round(sensor_dt / expected) - 1) if expected > 0 else 1
                self._stats["missing_estimate"] += estimated
                # Never integrate the last value across an unknown interval.
                # Apply drag for the unseen time, then integrate only a short step.
                step_cap = min(0.015, expected or 0.015)
                unseen = max(0.0, sensor_dt - step_cap)
                if settings.get("motion_mode", "integrate") == "integrate":
                    attenuation = math.exp(-float(settings.get("friction", 2.5)) * unseen)
                    self._velocity = [v * attenuation for v in self._velocity]
                dt = min(dt, step_cap)
            elif 0.001 <= sensor_dt <= 0.04:
                self._intervals.append(sensor_dt)
        self._last_sample_ts = timestamp
        self._last_sample_wall = now
        self._raw = raw
        self._sample_count += 1
        self._stats["samples"] += 1
        if self._intervals:
            self._stats["sensor_hz"] = 1.0 / median(self._intervals)

        if self._calibration == "collecting":
            self._calibration_samples.append(raw)
            self._stats["calibration_samples"] = len(self._calibration_samples)
            elapsed = now - self._calibration_started
            if len(self._calibration_samples) >= 100 or (elapsed >= 2.2 and len(self._calibration_samples) >= 25):
                self._bias = [float(median([v[i] for v in self._calibration_samples])) for i in range(3)]
                self._calibration = None
                self._stats["calibrating"] = False
                self._stats["bias"] = list(self._bias)
                self._velocity = [0.0, 0.0]
                self._fraction = [0.0, 0.0]
                self._filter.reset(tuple(a - b for a, b in zip(raw, self._bias)))
                self._message = "Calibration complete."
            return

        centered = [raw[i] - self._bias[i] for i in range(3)]
        if settings.get("adaptive_bias"):
            threshold = float(settings.get("stationary_threshold", 0.17))
            if max(abs(v) for v in centered) < threshold:
                rate = 0.10
                alpha = 1.0 - math.exp(-rate * dt)
                for i in range(3):
                    self._bias[i] += alpha * centered[i]
                    centered[i] = raw[i] - self._bias[i]
            self._stats["bias"] = list(self._bias)

        filtered = self._filter.process(tuple(centered), dt)
        self._filtered = tuple(filtered)
        self._stats["raw"] = list(raw)
        self._stats["filtered"] = list(filtered)
        values = {"x": filtered[0], "y": filtered[1], "z": filtered[2]}
        mx = values[settings.get("axis_x", "x")]
        my = values[settings.get("axis_y", "y")]
        if settings.get("invert_x"):
            mx = -mx
        if settings.get("invert_y", True):
            my = -my
        zone = float(settings.get("deadzone", 0.055)) if settings.get("deadzone_enabled", True) else 0.0
        mx = _soft_deadzone(mx, zone)
        my = _soft_deadzone(my, zone)
        curve = float(settings.get("response_curve", 1.0))
        mx, my = _shape(mx, curve), _shape(my, curve)

        signal_kind = settings.get("signal_kind", "linear_acceleration")
        speed_limit = float(settings.get("max_speed", 1800.0))
        if signal_kind == "gyroscope":
            speed_x = max(-speed_limit, min(speed_limit, mx * float(settings.get("direct_gain", 520.0))))
            speed_y = max(-speed_limit, min(speed_limit, my * float(settings.get("direct_gain", 520.0))))
            dx, dy = speed_x * dt, speed_y * dt
        else:
            if settings.get("motion_mode", "integrate") == "direct":
                speed_x = max(-speed_limit, min(speed_limit, mx * float(settings.get("direct_gain", 520.0))))
                speed_y = max(-speed_limit, min(speed_limit, my * float(settings.get("direct_gain", 520.0))))
                dx, dy = speed_x * dt, speed_y * dt
            else:
                gain = float(settings.get("gain", 12500.0))
                friction = float(settings.get("friction", 3.4))
                drag = math.exp(-friction * dt)
                self._velocity[0] = (self._velocity[0] + mx * gain * dt) * drag
                self._velocity[1] = (self._velocity[1] + my * gain * dt) * drag
                limit = float(settings.get("max_speed", 1800.0))
                self._velocity[0] = max(-limit, min(limit, self._velocity[0]))
                self._velocity[1] = max(-limit, min(limit, self._velocity[1]))
                dx, dy = self._velocity[0] * dt, self._velocity[1] * dt
                activity = max(abs(mx), abs(my))
                if activity < float(settings.get("stationary_threshold", 0.17)):
                    if self._stationary_since is None:
                        self._stationary_since = now
                    elif (now - self._stationary_since) * 1000.0 >= float(settings.get("stationary_reset_ms", 140)):
                        self._velocity = [0.0, 0.0]
                        dx = dy = 0.0
                else:
                    self._stationary_since = None
                self._stats["velocity"] = list(self._velocity)

        if settings.get("click_enabled") and settings.get("mouse_enabled"):
            if self._click_detector.update(tuple(centered), settings):
                if self._mouse.click():
                    self._stats["clicks"] += 1

        if settings.get("mouse_enabled") and not self._stats.get("calibrating"):
            self._pending_delta[0] += dx
            self._pending_delta[1] += dy
        self._stats["chart"] = (self._stats["chart"] + [list(filtered)])[-120:]
        self._stats["last_sample_age_ms"] = max(0.0, (time.perf_counter() - now) * 1000.0)

    def _flush_mouse(self, settings: dict) -> None:
        if not settings.get("mouse_enabled") or self._stats.get("calibrating"):
            self._fraction = [0.0, 0.0]
            return
        self._fraction[0] += self._pending_delta[0]
        self._fraction[1] += self._pending_delta[1]
        ix = math.trunc(self._fraction[0])
        iy = math.trunc(self._fraction[1])
        if not (ix or iy):
            return
        if self._mouse.move(ix, iy):
            self._fraction[0] -= ix
            self._fraction[1] -= iy
            self._move_count += 1
            self._stats["movement_chart"] = (self._stats["movement_chart"] + [min(200, math.hypot(ix, iy))])[-120:]
        else:
            self._stats["mouse_error"] = self._mouse.error
            self._fraction = [0.0, 0.0]

    def _update_rates(self, now: float) -> None:
        elapsed = now - self._request_window
        if elapsed >= 0.75:
            self._event_rates = {
                "poll_hz": self._request_count / elapsed,
                "sample_hz": self._sample_count / elapsed,
                "move_hz": self._move_count / elapsed,
            }
            self._request_count = self._sample_count = self._move_count = 0
            self._request_window = now
            self._stats.update(self._event_rates)

    def snapshot(self) -> dict:
        with self._lock:
            running = bool(self._thread and self._thread.is_alive() and not self._stop.is_set())
            stats = self._stats.copy()
            stats["chart"] = list(stats.get("chart", []))
            stats["movement_chart"] = list(stats.get("movement_chart", []))
            settings = self._settings.copy()
            return {
                "running": running,
                "message": self._message,
                "error": self._last_error,
                "stats": stats,
                "settings": settings,
                "connected": self._client is not None,
                "mouse_error": self._mouse.error,
            }
