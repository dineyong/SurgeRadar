"""Pure minute-bar detector. No network, clock, persistence or live integration."""
from collections import deque
from dataclasses import dataclass, fields
from datetime import timedelta
from decimal import Context, Decimal, InvalidOperation, localcontext

from .model import MinuteBar

DETECTOR_VERSION = "v0.1-no-rvol"


@dataclass(frozen=True)
class DetectorConfig:
    min_price: Decimal = Decimal("1.00")
    min_change_1m: Decimal = Decimal("0.008")
    min_change_3m: Decimal = Decimal("0.012")
    min_change_5m: Decimal = Decimal("0")  # strict >
    min_acceleration: Decimal = Decimal("0.004")
    min_volume_1m: Decimal = Decimal("5000")
    min_dollar_volume_5m: Decimal = Decimal("250000")
    candidate_change_1m: Decimal = Decimal("0.004")
    candidate_change_3m: Decimal = Decimal("0.008")

    def __post_init__(self):
        for field in fields(self):
            value = getattr(self, field.name)
            if isinstance(value, bool):
                raise ValueError(f"invalid threshold: {field.name}")
            try:
                value = Decimal(str(value))
            except (InvalidOperation, ValueError):
                raise ValueError(f"invalid threshold: {field.name}") from None
            if not value.is_finite() or value < 0:
                raise ValueError(f"threshold must be finite and nonnegative: {field.name}")
            object.__setattr__(self, field.name, value)

    def snapshot(self):
        return {field.name: str(getattr(self, field.name)) for field in fields(self)}


class Detector:
    def __init__(self, config: DetectorConfig | None = None):
        self.config = DetectorConfig() if config is None else config
        # State is isolated across data sources, never mix IEX and SIP history.
        self.history: dict[tuple[str, str, str], deque[MinuteBar]] = {}

    def evaluate(self, bar: MinuteBar) -> dict:
        key = (bar.provider, bar.feed, bar.ticker)
        history = self.history.setdefault(key, deque(maxlen=6))
        if history:
            delta = bar.event_at - history[-1].event_at
            if delta <= timedelta(0):
                raise ValueError("duplicate_or_out_of_order_bar")
            if delta != timedelta(minutes=1):
                history.clear()  # never bridge missing minutes or overnight gaps
        history.append(bar)
        bars = list(history)
        config = self.config
        # Use our own precision so caller Decimal context cannot change decisions.
        with localcontext(Context(prec=28)):
            changes = {f"change_{n}m": bar.close / bars[-n-1].close - 1
                       if len(bars) > n else None for n in (1, 3, 5)}
            acceleration = (changes["change_1m"] -
                            ((bars[-2].close / bars[-4].close - 1) / 2)
                            if len(bars) >= 4 else None)
            features = {**changes, "acceleration": acceleration, "volume_1m": bar.volume,
                        "volume_5m": sum((b.volume for b in bars[-5:]), Decimal(0))
                        if len(bars) >= 5 else None,
                        # Close * volume is an explicit bar-level approximation.
                        "dollar_volume_5m": sum((b.close * b.volume for b in bars[-5:]), Decimal(0))
                        if len(bars) >= 5 else None}
            candidate = ((changes["change_1m"] is not None and
                          changes["change_1m"] >= config.candidate_change_1m) or
                         (changes["change_3m"] is not None and
                          changes["change_3m"] >= config.candidate_change_3m))
            specifications = {
                "contiguous_history": (len(bars), ">=", 6),
                "price": (bar.close, ">=", config.min_price),
                "change_1m": (changes["change_1m"], ">=", config.min_change_1m),
                "change_3m": (changes["change_3m"], ">=", config.min_change_3m),
                "change_5m": (changes["change_5m"], ">", config.min_change_5m),
                "acceleration": (acceleration, ">=", config.min_acceleration),
                "volume_1m": (bar.volume, ">=", config.min_volume_1m),
                "dollar_volume_5m": (features["dollar_volume_5m"], ">=", config.min_dollar_volume_5m),
            }
            predicates = {name: {"passed": value is not None and
                                 (value > threshold if operator == ">" else value >= threshold),
                                 "value": str(value) if value is not None else None,
                                 "operator": operator, "threshold": str(threshold)}
                          for name, (value, operator, threshold) in specifications.items()}
        failed = [name for name, predicate in predicates.items() if not predicate["passed"]]
        detected = not failed  # candidate thresholds never gate a matching detection
        return {"event": "detection" if detected else "candidate_rejection" if candidate else "evaluation",
                "symbol": bar.ticker, "feature_at": bar.event_at.isoformat(),
                "detected_price": str(bar.close), "provider": bar.provider, "feed": bar.feed,
                "coverage_class": bar.coverage_class, "detector_version": DETECTOR_VERSION,
                "config": config.snapshot(), "candidate": candidate, "detected": detected,
                "features": {name: str(value) if value is not None else None
                             for name, value in features.items()},
                "predicates": predicates, "failed_predicates": failed}
