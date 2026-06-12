"""WebSocket broadcaster.

Connection flow:
  1. Client connects to /ws
  2. Server sends {"type": "connected", ...}
  3. Client may send {"type": "subscribe", "channels": [...]} (all by default)
  4. Server pushes {"type": "event", "channel": "...", ...} for each event
  5. Server pings every 20s; client may reply with {"type": "pong"} or
     {"type": "ping"} to keep things symmetric.
  6. Server enforces loopback-only by default (configurable in v2).
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.config import get_settings
from app.logging_config import correlation_id_var, get_logger
from app.services.event_bus import event_bus

router = APIRouter(tags=["ws"])

log = get_logger(__name__)

PING_INTERVAL_S = 20.0


async def _relay_task(ws: WebSocket, channel: str, q: asyncio.Queue) -> None:
    """Read from a subscribed event-bus queue and forward to the websocket.

    Returns when the websocket is closed (send fails) so the surrounding
    gather can clean up. Caller is responsible for unsubscribing the queue.
    """
    try:
        while True:
            event = await q.get()
            try:
                await ws.send_json(
                    {
                        "type": "event",
                        "channel": channel,
                        "payload": event.payload,
                        "event_id": event.event_id,
                        "ts": event.ts.isoformat(),
                    }
                )
            except (WebSocketDisconnect, RuntimeError):
                # Client gone. Exit silently.
                return
            except Exception as e:  # noqa: BLE001
                # Connection may have been reset; stop the relay.
                log.debug("ws.relay_send_failed", channel=channel, error=str(e))
                return
    except asyncio.CancelledError:
        return


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    settings = get_settings()
    if not settings.bind_public:
        client_host = (ws.client.host if ws.client else "") or ""
        if client_host not in ("127.0.0.1", "::1", "localhost"):
            log.warning("ws.reject_non_loopback", client=client_host)
            await ws.close(code=1008, reason="loopback only")
            return

    await ws.accept()
    cid = f"ws-{id(ws):x}"
    correlation_id_var.set(cid)
    log.info("ws.connected", client=ws.client.host if ws.client else "?")

    # channel -> (queue, relay_task)
    subs: dict[str, tuple[asyncio.Queue, asyncio.Task]] = {}
    reader_task: asyncio.Task | None = None
    pinger_task: asyncio.Task | None = None

    async def _subscribe(channel: str) -> None:
        if channel in subs:
            return
        q = event_bus.subscribe(channel)
        task = asyncio.create_task(_relay_task(ws, channel, q))
        subs[channel] = (q, task)

    def _unsubscribe(channel: str) -> None:
        entry = subs.pop(channel, None)
        if entry is None:
            return
        q, task = entry
        event_bus.unsubscribe(channel, q)
        task.cancel()

    async def _reader() -> None:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send_json({"type": "error", "message": "invalid json"})
                continue
            kind = msg.get("type")
            if kind == "subscribe":
                for ch in msg.get("channels", []) or []:
                    await _subscribe(ch)
            elif kind == "unsubscribe":
                for ch in msg.get("channels", []) or []:
                    _unsubscribe(ch)
            elif kind == "ping":
                await ws.send_json({"type": "pong", "ts": time.time()})
            elif kind == "pong":
                pass
            else:
                await ws.send_json({"type": "error", "message": f"unknown type: {kind}"})

    async def _pinger() -> None:
        try:
            while True:
                await asyncio.sleep(PING_INTERVAL_S)
                await ws.send_json({"type": "ping", "ts": time.time()})
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            return

    try:
        await ws.send_json({"type": "connected", "ts": time.time()})
        reader_task = asyncio.create_task(_reader())
        pinger_task = asyncio.create_task(_pinger())

        # Wait for any task to finish (e.g. reader exits when client
        # disconnects). Then cancel the rest.
        done, pending = await asyncio.wait(
            {reader_task, pinger_task, *(t for _, t in subs.values())},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
        for t in done:
            # Don't await cancelled tasks' exceptions
            if not t.cancelled() and t.exception() is not None:
                # Connection-closed errors are normal at teardown
                err = str(t.exception())
                if "1000" in err or "1001" in err or "1011" in err or "Connection" in err:
                    log.debug("ws.task_exit", error=err)
                else:
                    log.warning("ws.task_exception", error=err)
    except WebSocketDisconnect:
        log.info("ws.disconnected", client=ws.client.host if ws.client else "?")
    finally:
        correlation_id_var.set(None)
        for ch in list(subs.keys()):
            _unsubscribe(ch)
        for t in (reader_task, pinger_task):
            if t is not None and not t.done():
                t.cancel()


async def broadcast(channel: str, payload: Any) -> int:
    """Helper for sibling tracks — publishes to the in-process bus. The
    WebSocket layer is the consumer that fans events out to connected
    clients."""
    return await event_bus.publish(channel, payload)
