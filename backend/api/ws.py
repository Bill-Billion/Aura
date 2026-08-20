from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect

from backend.core.logging import log
from backend.models.schemas import WSMessage


@dataclass(slots=True)
class _ConnectionState:
    """Per-socket synchronization that survives a terminal send failure."""

    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    disconnected: asyncio.Event = field(default_factory=asyncio.Event)
    close_task: asyncio.Task[None] | None = None


class ConnectionManager:
    """Manages active WebSocket connections and message broadcasting."""

    SEND_TIMEOUT_SECONDS = 5.0
    CLOSE_TIMEOUT_SECONDS = 1.0

    def __init__(self) -> None:
        self.active: list[WebSocket] = []
        self._states: dict[WebSocket, _ConnectionState] = {}

    async def accept(self, ws: WebSocket) -> None:
        """Accept a WebSocket before registering it for broadcasts."""
        await ws.accept()
        self._states.setdefault(ws, _ConnectionState())

    async def initialize(
        self,
        ws: WebSocket,
        *,
        full_state: dict | None = None,
        initial_messages_factory: Callable[[], Sequence[WSMessage]] | None = None,
        registration_lock: asyncio.Lock | None = None,
    ) -> bool:
        """Register ``ws`` and deliver its initial messages as one ordered batch.

        The connection becomes visible to broadcasters while its per-socket lock
        is held.  Concurrent deltas therefore queue behind the initial snapshot
        instead of overtaking it or being overwritten by it.  When a
        ``registration_lock`` is supplied, registration and snapshot capture are
        protected by that lock, but the network writes happen after it is
        released.
        """
        state = self._states.setdefault(ws, _ConnectionState())
        lock_acquired = False
        messages: list[WSMessage] = []
        try:
            if registration_lock is not None:
                # Global state lock first, socket lock second.  This matches
                # command handlers that make a state decision and then send,
                # avoiding a lock inversion with concurrent broadcasts.
                async with registration_lock:
                    await state.send_lock.acquire()
                    lock_acquired = True
                    if state.disconnected.is_set():
                        return False
                    if ws not in self.active:
                        self.active.append(ws)
                    if full_state is not None:
                        messages.append(WSMessage(type="STATE_FULL", payload=full_state))
                    if initial_messages_factory is not None:
                        messages.extend(initial_messages_factory())
            else:
                await state.send_lock.acquire()
                lock_acquired = True
                if state.disconnected.is_set():
                    return False
                if ws not in self.active:
                    self.active.append(ws)
                if full_state is not None:
                    messages.append(WSMessage(type="STATE_FULL", payload=full_state))
                if initial_messages_factory is not None:
                    messages.extend(initial_messages_factory())

            for message in messages:
                if not await self._send_unlocked(ws, state, message):
                    return False
            log.info("ws_connected", total_active=len(self.active))
            return True
        finally:
            if lock_acquired:
                state.send_lock.release()

    async def connect(self, ws: WebSocket, full_state: dict | None = None) -> None:
        """Accept, register, and optionally initialize a WebSocket connection."""
        await self.accept(ws)
        await self.initialize(ws, full_state=full_state)

    def disconnect(self, ws: WebSocket) -> None:
        """Mark a WebSocket terminal and remove it from broadcasts.

        Its state intentionally remains until the endpoint calls :meth:`close`.
        Keeping the terminal event and the original send lock prevents a failed
        socket from being recreated by a later direct error response.
        """
        state = self._states.get(ws)
        changed = ws in self.active or (
            state is not None and not state.disconnected.is_set()
        )
        if ws in self.active:
            self.active.remove(ws)
        if state is not None:
            state.disconnected.set()
        if changed:
            log.info("ws_disconnected", total_active=len(self.active))

    async def receive_json(self, ws: WebSocket) -> Any:
        """Receive one frame, aborting promptly when an outbound send fails."""
        state = self._states.get(ws)
        if state is None or state.disconnected.is_set() or ws not in self.active:
            raise WebSocketDisconnect(code=1011, reason="connection_terminated")

        receive_task = asyncio.create_task(ws.receive_json())
        terminal_task = asyncio.create_task(state.disconnected.wait())
        try:
            done, _ = await asyncio.wait(
                {receive_task, terminal_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            # A simultaneous inbound frame may never revive a socket whose
            # outbound path has already failed.
            if terminal_task in done:
                raise WebSocketDisconnect(code=1011, reason="send_failed")
            return receive_task.result()
        finally:
            for task in (receive_task, terminal_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(receive_task, terminal_task, return_exceptions=True)

    async def close(self, ws: WebSocket, *, code: int = 1000) -> None:
        """Best-effort close and release all retained per-socket state."""
        state = self._states.get(ws)
        self.disconnect(ws)
        if state is None:
            return

        task = state.close_task
        if task is None:
            task = asyncio.create_task(self._best_effort_close(ws, code=code))
            state.close_task = task
        await asyncio.shield(task)
        if self._states.get(ws) is state:
            self._states.pop(ws, None)

    async def _best_effort_close(self, ws: WebSocket, *, code: int) -> None:
        try:
            await asyncio.wait_for(
                ws.close(code=code),
                timeout=self.CLOSE_TIMEOUT_SECONDS,
            )
        except Exception:
            log.debug("ws_close_failed")

    def _schedule_failed_close(self, ws: WebSocket, state: _ConnectionState) -> None:
        if state.close_task is None:
            state.close_task = asyncio.create_task(
                self._best_effort_close(ws, code=1011),
                name="close-failed-websocket",
            )

    async def send(self, ws: WebSocket, msg: WSMessage) -> bool:
        """Send to a registered client; terminal clients can never be revived."""
        state = self._states.get(ws)
        if state is None:
            return False
        async with state.send_lock:
            if state.disconnected.is_set() or ws not in self.active:
                return False
            return await self._send_unlocked(ws, state, msg)

    async def _send_unlocked(
        self,
        ws: WebSocket,
        state: _ConnectionState,
        msg: WSMessage,
    ) -> bool:
        """Deliver one message while the caller owns the socket send lock."""
        if state.disconnected.is_set():
            return False
        try:
            await asyncio.wait_for(
                ws.send_json(msg.model_dump()),
                timeout=self.SEND_TIMEOUT_SECONDS,
            )
            return True
        except Exception:
            log.warning("ws_send_failed")
            self.disconnect(ws)
            self._schedule_failed_close(ws, state)
            return False

    async def broadcast(self, msg: WSMessage) -> None:
        """Broadcast a WSMessage to all active connections. Remove dead ones."""
        before = len(self.active)
        await asyncio.gather(*(self.send(ws, msg) for ws in tuple(self.active)))
        removed = before - len(self.active)
        if removed:
            log.warning("ws_broadcast_removed_dead", count=removed)
