"""Console adapter for the same deterministic core used by offline replay."""
from datetime import datetime, timedelta, timezone

from .detector import Detector


class LiveDetector:
    def __init__(self, log):
        self.detector = Detector()
        self.log = log
        self.bars_evaluated = self.candidates = self.detections = 0
        self.candidate_rejections = self.detector_errors = 0
        self.history_gaps = self.stream_discontinuities = 0

    def observe(self, bar):
        key = (bar.provider, bar.feed, bar.ticker)
        history = self.detector.history.get(key)
        if history and bar.event_at - history[-1].event_at > timedelta(minutes=1):
            self.history_gaps += 1
            self.log.emit("detector_history_gap", symbol=bar.ticker,
                          previous_feature_at=history[-1].event_at,
                          feature_at=bar.event_at, reason="non_contiguous_minutes")
        try:
            result = self.detector.evaluate(bar)
        except Exception as exc:
            self.detector_errors += 1
            # Core rejects duplicate/out-of-order bars before changing state.
            # Other failures invalidate this symbol's history before continuing.
            out_of_order = isinstance(exc, ValueError) and exc.args == ("duplicate_or_out_of_order_bar",)
            if not out_of_order:
                self.detector.history.pop(key, None)
            self.log.emit("detector_error", symbol=bar.ticker, feature_at=bar.event_at,
                          reason="duplicate_or_out_of_order_bar" if out_of_order else "evaluation_failed",
                          error_type=type(exc).__name__)
            return
        self.bars_evaluated += 1
        self.candidates += int(result["candidate"])
        self.detections += int(result["detected"])
        self.candidate_rejections += int(result["event"] == "candidate_rejection")
        if result["event"] in ("detection", "candidate_rejection"):
            fields = {key: value for key, value in result.items() if key != "event"}
            self.log.emit("live_detection" if result["detected"] else "live_candidate_rejection",
                          **fields, received_at=bar.received_at,
                          processed_at=datetime.now(timezone.utc),
                          receive_lag_seconds=bar.lag_seconds)

    def discontinuity(self):
        self.stream_discontinuities += 1
        cleared = len(self.detector.history)
        self.detector.history.clear()
        self.log.emit("detector_state_reset", reason="stream_discontinuity",
                      cleared_symbol_states=cleared)

    def summary(self):
        return {"bars_evaluated": self.bars_evaluated, "candidates": self.candidates,
                "detections": self.detections, "candidate_rejections": self.candidate_rejections,
                "symbols_with_detector_state": len(self.detector.history),
                "detector_errors": self.detector_errors,
                "history_gaps": self.history_gaps,
                "stream_discontinuities": self.stream_discontinuities,
                "detector_continuity": "discontinuous" if self.history_gaps or self.stream_discontinuities
                else "no_observed_discontinuity"}
