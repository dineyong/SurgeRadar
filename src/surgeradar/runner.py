import asyncio
from collections import deque
from contextlib import AsyncExitStack
from datetime import datetime, timezone
import time
from .config import Config
from .model import Malformed, normalize_bar, parse_frame
from .metrics import Metrics
from .providers import Provider, ProviderError, check_error


class DiagnosticError(Exception):
    pass


def decode(raw, metrics, *, count_frame=True):
    if count_frame:
        metrics.frames += 1
    try:
        items = parse_frame(raw)
    except Malformed:
        metrics.malformed += 1
        return []
    metrics.messages += len(items)
    return items


async def handshake(socket, provider, symbols, log, metrics):
    async def wait_for(kind, message=None):
        # Bounded handshake, even if an endpoint keeps sending unrelated messages.
        async with asyncio.timeout(8):
            while True:
                for item in decode(await socket.recv(), metrics):
                    if not isinstance(item, dict):
                        metrics.malformed += 1
                        continue
                    check_error(item)
                    if item.get("T") == kind and (message is None or item.get("msg") == message):
                        return item
                    metrics.ignored += 1

    await wait_for("success", "connected")
    log.emit("connected")
    await socket.send(provider.authentication())
    await wait_for("success", "authenticated")
    log.emit("sip_entitlement_verified", evidence="authenticated_on_v2_sip")
    await socket.send(provider.subscription(symbols))
    acknowledgement = await wait_for("subscription")
    ack = acknowledgement.get("bars")
    if not isinstance(ack, list) or not all(isinstance(s, str) for s in ack):
        raise DiagnosticError("malformed_subscription_ack")
    if not set(symbols).issubset(ack):
        raise DiagnosticError("subscription_not_acknowledged")
    # Log requested symbols only, not arbitrary upstream strings.
    log.emit("subscription_ack", symbols=list(symbols), broad=symbols == ("*",))


async def stream(socket, metrics, log, config, deadline, *, log_bars):
    queue = asyncio.Queue(maxsize=config.queue_size)
    enqueued_at = deque()

    async def reader():
        while True:
            raw = await socket.recv()
            metrics.frames += 1
            received = datetime.now(timezone.utc)
            received_mono = time.monotonic()
            try:
                queue.put_nowait((raw, received, received_mono))
            except asyncio.QueueFull:
                metrics.overflow += 1
                log.emit("queue_overflow", capacity=config.queue_size)
                raise DiagnosticError("queue_overflow")
            enqueued_at.append(received_mono)
            metrics.queue_high_water = max(metrics.queue_high_water, queue.qsize())

    async def consumer():
        while True:
            raw, received, received_mono = await queue.get()
            enqueued_at.popleft()
            metrics.queue_ages.append(time.monotonic() - received_mono)
            for item in decode(raw, metrics, count_frame=False):
                if not isinstance(item, dict):
                    metrics.malformed += 1
                    continue
                check_error(item)
                if item.get("T") != "b":
                    metrics.ignored += 1
                    continue
                try:
                    bar = normalize_bar(item, received)
                except Malformed:
                    metrics.malformed += 1
                    continue
                metrics.observe(bar)
                if log_bars:
                    log.emit("minute_bar", ticker=bar.ticker,
                             event_at=bar.event_at, received_at=bar.received_at,
                             receive_lag_seconds=bar.lag_seconds)
            queue.task_done()

    async def reporter():
        while True:
            await asyncio.sleep(config.report_seconds)
            age = time.monotonic()-enqueued_at[0] if enqueued_at else 0
            metrics.report(log, queue.qsize(), age)

    tasks = [asyncio.create_task(fn()) for fn in (reader, consumer, reporter)]
    try:
        done, _ = await asyncio.wait(tasks, timeout=max(0, deadline-time.monotonic()),
                                     return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()  # propagate fatal errors / disconnects
    finally:
        # Stop intake first; drain already received frames without unbounded waiting.
        tasks[0].cancel()
        await asyncio.gather(tasks[0], return_exceptions=True)
        if not tasks[1].done():
            try:
                await asyncio.wait_for(queue.join(), timeout=2)
            except TimeoutError:
                log.emit("queue_drain_incomplete", remaining=queue.qsize())
                metrics.overflow += 1
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        age = time.monotonic()-enqueued_at[0] if enqueued_at else 0
        metrics.report(log, queue.qsize(), age)


async def run(config: Config, provider: Provider, log, mode: str) -> int:
    from websockets.exceptions import ConnectionClosed, InvalidHandshake
    metrics = Metrics()
    symbols = ("*",) if mode == "broad" else config.symbols
    deadline = time.monotonic() + config.duration
    attempts = 0
    log.emit("starting", mode=mode, symbols=list(symbols), credentials_present=True,
             duration_seconds=config.duration)
    result = 3
    try:
        while time.monotonic() < deadline:
            try:
                async with AsyncExitStack() as stack:
                    # Limit connection/auth while leaving stream cleanup outside timeout.
                    async with asyncio.timeout(max(0, deadline-time.monotonic())):
                        socket = await stack.enter_async_context(provider.open())
                        await handshake(socket, provider, symbols, log, metrics)
                    if attempts:
                        metrics.reconnects += 1
                        log.emit("reconnected", attempt=attempts,
                                 continuity="not_verified_no_backfill_in_v0_1")
                    if mode == "check":
                        result = 0
                        log.emit("check_complete", sip_entitled=True,
                                 ingestion_verified=False)
                        break
                    await stream(socket, metrics, log, config, deadline,
                                 log_bars=mode == "smoke")
                    result = 0 if metrics.bars and not metrics.overflow else 5
                    break
            except ProviderError as exc:
                log.emit("provider_error", code=exc.code, reason=exc.reason,
                         sip_entitled=False if exc.code == 409 else None)
                if not exc.retryable:
                    result = 4 if exc.code == 409 else 3
                    break
                metrics.disconnects += 1
                log.emit("disconnected", reason=exc.reason)
            except DiagnosticError as exc:
                log.emit("diagnostic_failed", reason=str(exc))
                result = 3
                break
            except (OSError, ConnectionClosed, InvalidHandshake, TimeoutError) as exc:
                metrics.disconnects += 1
                log.emit("disconnected", error_type=type(exc).__name__,
                         reason="transport_or_handshake_timeout")
            if attempts >= config.max_reconnects or time.monotonic() >= deadline:
                log.emit("reconnect_exhausted", attempts=attempts)
                result = 3
                break
            attempts += 1
            delay = min(2 ** (attempts-1), 30, max(0, deadline-time.monotonic()))
            log.emit("reconnect_scheduled", attempt=attempts, delay_seconds=delay)
            await asyncio.sleep(delay)
        if mode != "check":
            log.emit("smoke_result", bars_observed=metrics.bars,
                     status="bars_observed" if result == 0 else "incomplete_or_no_bars",
                     continuous=metrics.disconnects == 0 and metrics.overflow == 0,
                     broad_capacity_verified=False)
        return result
    finally:
        metrics.report(log, 0, 0, final=True)
        log.emit("disconnected", reason="runner_stopped")
