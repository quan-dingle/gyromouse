from __future__ import annotations

from dataclasses import dataclass
import math
import re
import time
from urllib.parse import urlsplit, urlunsplit, quote, urlencode

import requests


@dataclass(frozen=True)
class MappingGroup:
    key: str
    label: str
    kind: str
    x: str
    y: str
    z: str
    t: str = ""

    def as_dict(self) -> dict:
        return {"key": self.key, "label": self.label, "kind": self.kind,
                "mapping": {"x_buffer": self.x, "y_buffer": self.y,
                            "z_buffer": self.z, "time_buffer": self.t}}


def normalize_url(value: str) -> str:
    raw = value.strip()
    if not raw:
        raise ValueError("Enter the phone's phyphox remote URL first.")
    if "://" not in raw:
        raw = "http://" + raw
    parsed = urlsplit(raw)
    path = parsed.path.rstrip("/")
    if path.lower() in {"/get", "/config", "/meta", "/control"}:
        path = path.rsplit("/", 1)[0]
    return urlunsplit((parsed.scheme or "http", parsed.netloc, path, "", "")).rstrip("/")


def _normal(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.casefold())


def _buffer_names(config: dict) -> list[str]:
    values = config.get("buffers", [])
    names = []
    if isinstance(values, dict):
        names = [str(k) for k in values]
    elif isinstance(values, list):
        for item in values:
            if isinstance(item, str):
                names.append(item)
            elif isinstance(item, dict) and item.get("name"):
                names.append(str(item["name"]))
    return list(dict.fromkeys(names))


def detect_groups(config: dict) -> list[MappingGroup]:
    names = _buffer_names(config)
    lookup = {_normal(name): name for name in names}

    specs = [
        ("linear_acceleration", "Linear acceleration", "linear_acceleration",
         ("lax", "linearaccelerationx", "linaccx", "linearaccx", "accelerationwithoutgravityx"),
         ("lay", "linearaccelerationy", "linaccy", "linearaccy", "accelerationwithoutgravityy"),
         ("laz", "linearaccelerationz", "linaccz", "linearaccz", "accelerationwithoutgravityz"),
         ("lat", "linearaccelerationtime", "linacct", "linearaccelerationt")),
        ("accelerometer_custom", "Accelerometer / custom accX", "accelerometer",
         ("accx", "accelerometerx", "accelerationx", "ax"),
         ("accy", "accelerometery", "accelerationy", "ay"),
         ("accz", "accelerometerz", "accelerationz", "az"),
         ("acct", "accelerationtime", "at", "time")),
        ("accelerometer_short", "Accelerometer (ax / ay / az)", "accelerometer",
         ("ax", "accelerationx", "accelerometerx"),
         ("ay", "accelerationy", "accelerometery"),
         ("az", "accelerationz", "accelerometerz"),
         ("at", "accelerationtime", "time")),
        ("gyroscope", "Gyroscope", "gyroscope",
         ("gx", "gyrox", "gyroscopex", "angularvelocityx"),
         ("gy", "gyroy", "gyroscopey", "angularvelocityy"),
         ("gz", "gyroz", "gyroscopez", "angularvelocityz"),
         ("gt", "gyrotime", "gyroscopetime", "time")),
    ]
    out = []
    seen = set()
    for key, label, kind, xs, ys, zs, ts in specs:
        x = next((lookup[c] for c in xs if c in lookup), "")
        y = next((lookup[c] for c in ys if c in lookup), "")
        z = next((lookup[c] for c in zs if c in lookup), "")
        t = next((lookup[c] for c in ts if c in lookup), "")
        if x and y and z and (x, y, z) not in seen:
            out.append(MappingGroup(key, label, kind, x, y, z, t))
            seen.add((x, y, z))
    if not out:
        # A deliberately conservative fallback: detect common x/y/z suffixes
        # for experiments whose buffer names do not follow phyphox conventions.
        for prefix in ("", "acc", "gyro", "gyr", "linearacceleration", "linacc"):
            for sx, sy, sz in (("x", "y", "z"), ("X", "Y", "Z")):
                vals = [lookup.get(_normal(prefix + axis), "") for axis in (sx, sy, sz)]
                if all(vals) and tuple(vals) not in seen:
                    out.append(MappingGroup("custom_" + prefix, f"Custom {prefix or 'XYZ'}", "custom", *vals, ""))
                    seen.add(tuple(vals))
    return out


def _numbers(entry: dict | None) -> list[float]:
    if not isinstance(entry, dict):
        return []
    raw = entry.get("buffer", [])
    if not isinstance(raw, list):
        raw = [raw]
    values = []
    for item in raw:
        try:
            number = float(item)
        except (TypeError, ValueError):
            values.append(float("nan"))
            continue
        values.append(number)
    return values


class PhyphoxClient:
    def __init__(self, base_url: str):
        self.base_url = normalize_url(base_url)
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Gyromouse/2.0", "Connection": "keep-alive"})
        self.config: dict = {}
        self.meta: dict = {}
        self.names: list[str] = []
        self.groups: list[MappingGroup] = []
        self.session_id: str | None = None
        self.last_timestamp: float | None = None
        self.last_values: tuple[float, float, float] | None = None
        self._primed = False

    def _json(self, path: str, params=None, timeout=(0.25, 0.65)) -> tuple[dict, float]:
        started = time.perf_counter()
        url = f"{self.base_url}/{path.lstrip('/')}"
        if isinstance(params, list) and any(value is None for _, value in params):
            parts = []
            for key, value in params:
                if value is None:
                    parts.append(quote(str(key), safe=""))
                else:
                    parts.append(urlencode([(key, value)]))
            url = url + ("?" if "?" not in url else "&") + "&".join(parts)
            response = self.session.get(url, timeout=timeout)
        else:
            response = self.session.get(url, params=params, timeout=timeout)
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError(f"phyphox returned non-JSON from /{path.lstrip('/')}") from exc
        return payload, (time.perf_counter() - started) * 1000.0

    def discover(self) -> dict:
        self.config, rtt = self._json("/config", timeout=(0.4, 1.3))
        try:
            self.meta, _ = self._json("/meta", timeout=(0.25, 0.8))
        except requests.RequestException:
            self.meta = {}
        self.names = _buffer_names(self.config)
        self.groups = detect_groups(self.config)
        return {
            "title": self.config.get("localTitle") or self.config.get("title") or "phyphox experiment",
            "buffers": [{"name": name, "size": self._buffer_size(name)} for name in self.names],
            "groups": [g.as_dict() for g in self.groups],
            "meta": self.meta,
            "rtt_ms": round(rtt, 2),
        }

    def _buffer_size(self, name: str) -> int:
        values = self.config.get("buffers", [])
        if isinstance(values, list):
            for item in values:
                if isinstance(item, dict) and item.get("name") == name:
                    try:
                        return int(item.get("size", 0))
                    except (TypeError, ValueError):
                        return 0
        if isinstance(values, dict) and isinstance(values.get(name), dict):
            try:
                return int(values[name].get("size", 0))
            except (TypeError, ValueError):
                return 0
        return 0

    def status(self, status: dict) -> dict:
        incoming = status.get("session")
        changed = self.session_id is not None and incoming is not None and incoming != self.session_id
        if incoming is not None:
            self.session_id = incoming
        if changed:
            self.last_timestamp = None
            self.last_values = None
            self._primed = False
        return {"measuring": bool(status.get("measuring", False)), "session_changed": changed,
                "session": self.session_id}

    def control(self, command: str) -> bool:
        payload, _ = self._json("/control", params={"cmd": command}, timeout=(0.25, 0.8))
        return bool(payload.get("result", False))

    def latest(self, mapping: dict) -> dict:
        xname, yname, zname = (mapping.get(k, "") for k in ("x_buffer", "y_buffer", "z_buffer"))
        tname = mapping.get("time_buffer", "")
        if not all((xname, yname, zname)):
            raise ValueError("Map the X, Y and Z sensor buffers before starting.")
        wanted = list(dict.fromkeys(([tname] if tname else []) + [xname, yname, zname]))
        params = [(name, None) for name in wanted]
        payload, rtt = self._json("/get", params=params)
        stat = self.status(payload.get("status", {}))
        buffers = payload.get("buffer", {})
        x, y, z = (_numbers(buffers.get(n)) for n in (xname, yname, zname))
        tv = _numbers(buffers.get(tname)) if tname else []
        if not (x and y and z):
            if not stat["measuring"]:
                return {"samples": [], "status": stat, "rtt_ms": rtt, "primed": False}
            raise RuntimeError("The selected buffers returned no numeric samples. Check the mapping and start the experiment.")
        if tname and not tv:
            raise RuntimeError(f"Timestamp buffer '{tname}' returned no data. Try latest-sample mode or change the time mapping.")
        sample = (tv[-1] if tv else time.perf_counter(), x[-1], y[-1], z[-1])
        return {"samples": [sample], "status": stat, "rtt_ms": rtt, "primed": False}

    def next_batch(self, mapping: dict, mode: str) -> dict:
        tname = mapping.get("time_buffer", "")
        if mode != "drain" or not tname:
            return self.latest(mapping)
        xname, yname, zname = (mapping.get(k, "") for k in ("x_buffer", "y_buffer", "z_buffer"))
        if not all((xname, yname, zname)):
            raise ValueError("Map the X, Y and Z sensor buffers before starting.")

        if self.last_timestamp is None or not self._primed:
            result = self.latest(mapping)
            if result["samples"] and result["status"].get("measuring"):
                self.last_timestamp = result["samples"][-1][0]
                self.last_values = result["samples"][-1][1:]
                self._primed = True
                result["primed"] = True
            result["samples"] = []
            return result

        cursor = float(self.last_timestamp)
        params = [
            (tname, str(cursor)),
            (xname, f"{cursor}|{tname}"),
            (yname, f"{cursor}|{tname}"),
            (zname, f"{cursor}|{tname}"),
        ]
        payload, rtt = self._json("/get", params=params)
        stat = self.status(payload.get("status", {}))
        if stat.get("session_changed"):
            return {"samples": [], "status": stat, "rtt_ms": rtt, "primed": False}
        buffers = payload.get("buffer", {})
        times = _numbers(buffers.get(tname))
        xs = _numbers(buffers.get(xname))
        ys = _numbers(buffers.get(yname))
        zs = _numbers(buffers.get(zname))
        count = min(len(times), len(xs), len(ys), len(zs))
        rows = []
        newest = cursor
        newest_values = None
        for i in range(count):
            t, x, y, z = times[i], xs[i], ys[i], zs[i]
            if not math.isfinite(t) or t <= cursor or t <= newest:
                continue
            newest = t
            if not all(math.isfinite(v) for v in (x, y, z)):
                continue
            rows.append((t, x, y, z))
            newest_values = (x, y, z)
        if newest > cursor:
            self.last_timestamp = newest
            if newest_values is not None:
                self.last_values = newest_values
        cap = min((self._buffer_size(n) for n in (tname, xname, yname, zname) if self._buffer_size(n) > 0), default=0)
        overflow_suspected = bool(cap and count >= cap - 1)
        return {"samples": rows, "status": stat, "rtt_ms": rtt,
                "overflow_suspected": overflow_suspected, "batch_size": count, "primed": False}
