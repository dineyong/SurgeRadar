from collections import deque
from datetime import timezone
import math
import time
from .model import MinuteBar


def percentile(values, q):
    if not values:
        return None
    values = sorted(values)
    position = (len(values) - 1) * q
    lower = math.floor(position)
    upper = math.ceil(position)
    return values[lower] + (values[upper] - values[lower]) * (position - lower)


class Metrics:
    def __init__(self):
        self.started = time.monotonic()
        self.last_report = self.started
        self.frames = self.messages = self.bars = self.malformed = self.ignored = 0
        self.previous_messages = self.previous_bars = 0
        self.symbols: set[str] = set()
        self.lags = deque(maxlen=100_000)
        self.queue_ages = deque(maxlen=100_000)
        self.queue_high_water = 0
        self.overflow = self.reconnects = self.disconnects = 0
        self.last_event_at = self.last_received_at = None
        self.minutes: dict[str, tuple[int, set[str]]] = {}
        import psutil
        self.process = psutil.Process()
        self.process.cpu_percent()  # establish interval baseline

    def observe(self, bar: MinuteBar):
        self.bars += 1
        self.symbols.add(bar.ticker)
        self.lags.append(bar.lag_seconds)
        self.last_event_at, self.last_received_at = bar.event_at, bar.received_at
        minute = bar.event_at.astimezone(timezone.utc).isoformat()
        count, symbols = self.minutes.get(minute, (0, set()))
        symbols.add(bar.ticker)
        self.minutes[minute] = count + 1, symbols
        # Bounded diagnostic history; aggregate counters remain cumulative.
        if len(self.minutes) > 120:
            oldest = min(self.minutes)
            del self.minutes[oldest]

    def report(self, log, queue_depth: int, queue_age: float, *, final=False):
        now = time.monotonic()
        elapsed = max(now - self.last_report, 1e-9)
        log.emit("summary", final=final, elapsed_seconds=now-self.started,
                 frames_received=self.frames, messages_received=self.messages,
                 bars_received=self.bars, unique_symbols=len(self.symbols),
                 last_event_at=self.last_event_at, last_received_at=self.last_received_at,
                 malformed_messages=self.malformed, ignored_messages=self.ignored,
                 messages_per_second=(self.messages-self.previous_messages)/elapsed,
                 bars_per_second=(self.bars-self.previous_bars)/elapsed,
                 lag_p50_seconds=percentile(self.lags, .50),
                 lag_p95_seconds=percentile(self.lags, .95),
                 lag_p99_seconds=percentile(self.lags, .99),
                 lag_sample_count=len(self.lags),
                 queue_depth=queue_depth, queue_oldest_age_seconds=queue_age,
                 queue_high_water=self.queue_high_water,
                 queue_age_p99_seconds=percentile(self.queue_ages, .99),
                 queue_overflows=self.overflow, reconnects=self.reconnects,
                 disconnects=self.disconnects,
                 cpu_percent=self.process.cpu_percent(),
                 rss_bytes=self.process.memory_info().rss)
        self.previous_messages, self.previous_bars = self.messages, self.bars
        self.last_report = now
        # Latest event minute is provisional: arrivals can straddle report boundaries.
        latest = max(self.minutes, default=None)
        for minute, (count, symbols) in sorted(self.minutes.items()):
            if final or minute != latest:
                log.emit("event_minute", minute_start=minute,
                         bars_per_minute=count, unique_symbols_per_minute=len(symbols),
                         provisional=final and minute == latest)
        # These counters are snapshots, not increments; consumers must not sum repeats.
