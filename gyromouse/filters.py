from __future__ import annotations

from collections import deque
from statistics import median
from typing import Callable, Iterable
import math

Vector = tuple[float, float, float]


def _alpha(cutoff_hz: float, dt: float) -> float:
    cutoff_hz = max(0.001, float(cutoff_hz))
    dt = max(1e-5, float(dt))
    tau = 1.0 / (2.0 * math.pi * cutoff_hz)
    return 1.0 / (1.0 + tau / dt)


class MotionFilter:
    key = "base"
    label = "Base"
    description = "Base filter interface."

    def __init__(self, settings: dict):
        self.settings = settings

    def reset(self, seed: Vector | None = None) -> None:
        pass

    def process(self, value: Vector, dt: float) -> Vector:
        return value


class RawFilter(MotionFilter):
    key = "raw"
    label = "Raw · zero added lag"
    description = "No smoothing. Use to inspect the actual remote buffer stream."


class EMAFilter(MotionFilter):
    key = "ema"
    label = "Adaptive-free low-pass"
    description = "A timestamp-aware exponential low-pass with a tunable cutoff."

    def reset(self, seed: Vector | None = None) -> None:
        self.last = list(seed) if seed is not None else None

    def process(self, value: Vector, dt: float) -> Vector:
        cutoff = float(self.settings.get("ema_cutoff", 12.0))
        a = _alpha(cutoff, dt)
        if self.last is None:
            self.last = list(value)
        else:
            self.last = [p + a * (v - p) for p, v in zip(self.last, value)]
        return tuple(self.last)


class OneEuroFilter(MotionFilter):
    key = "one_euro"
    label = "One Euro · adaptive"
    description = "Strong near rest, increasingly responsive during fast movement. Usually the best balance."

    def reset(self, seed: Vector | None = None) -> None:
        self.last = list(seed) if seed is not None else None
        self.last_derivative = [0.0, 0.0, 0.0]

    def process(self, value: Vector, dt: float) -> Vector:
        min_cutoff = float(self.settings.get("min_cutoff", 1.35))
        beta = float(self.settings.get("beta", 0.08))
        derivative_cutoff = float(self.settings.get("derivative_cutoff", 1.0))
        if self.last is None:
            self.last = list(value)
            self.last_derivative = [0.0, 0.0, 0.0]
            return value

        a_d = _alpha(derivative_cutoff, dt)
        filtered = []
        derivatives = []
        for i, current in enumerate(value):
            derivative = (current - self.last[i]) / max(dt, 1e-5)
            d_filtered = self.last_derivative[i] + a_d * (derivative - self.last_derivative[i])
            cutoff = min_cutoff + beta * abs(d_filtered)
            a = _alpha(cutoff, dt)
            filtered.append(self.last[i] + a * (current - self.last[i]))
            derivatives.append(d_filtered)
        self.last = filtered
        self.last_derivative = derivatives
        return tuple(filtered)


class MedianOneEuroFilter(OneEuroFilter):
    key = "median_one_euro"
    label = "Median + One Euro"
    description = "Rejects isolated sensor spikes first, then adapts smoothing to movement speed."

    def reset(self, seed: Vector | None = None) -> None:
        super().reset(seed)
        self.window = [deque(maxlen=3) for _ in range(3)]
        if seed is not None:
            for i, axis in enumerate(self.window):
                axis.append(seed[i])

    def process(self, value: Vector, dt: float) -> Vector:
        if not hasattr(self, "window"):
            self.reset()
        med = []
        for i, current in enumerate(value):
            self.window[i].append(current)
            med.append(float(median(self.window[i])))
        return super().process(tuple(med), dt)


FILTERS: dict[str, type[MotionFilter]] = {
    cls.key: cls for cls in (RawFilter, EMAFilter, OneEuroFilter, MedianOneEuroFilter)
}


def make_filter(key: str, settings: dict) -> MotionFilter:
    cls = FILTERS.get(key, OneEuroFilter)
    instance = cls(settings)
    instance.reset()
    return instance


def filter_catalog() -> list[dict[str, str]]:
    return [
        {"key": cls.key, "label": cls.label, "description": cls.description}
        for cls in FILTERS.values()
    ]
