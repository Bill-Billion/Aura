from __future__ import annotations

import asyncio
from fastapi import WebSocket

from backend.core.logging import log
from backend.models.schemas import WSMessage


class ConnectionManager:
    """Manages active WebSocket connections and message broadcasting."""

    def __init__(self) -> None:
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket, full_state: dict | None = None) -> None:
        """Accept a new WebSocket connection and optionally send full state."""
        await ws.accept()
        self.active.append(ws)
        if full_state is not None:
            msg = WSMessage(type="STATE_FULL", payload=full_state)
            await self.send(ws, msg)
        log.info("ws_connected", total_active=len(self.active))

    def disconnect(self, ws: WebSocket) -> None:
        """Remove a WebSocket from the active list."""
        if ws in self.active:
            self.active.remove(ws)
        log.info("ws_disconnected", total_active=len(self.active))

    async def send(self, ws: WebSocket, msg: WSMessage) -> None:
        """Send a WSMessage as JSON to a single client. Disconnect on error."""
        try:
            await ws.send_json(msg.model_dump())
        except Exception:
            log.warning("ws_send_failed")
            self.disconnect(ws)

    async def broadcast(self, msg: WSMessage) -> None:
        """Broadcast a WSMessage to all active connections. Remove dead ones."""
        dead: list[WebSocket] = []
        for ws in self.active:
            try:
                await ws.send_json(msg.model_dump())
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)
        if dead:
            log.warning("ws_broadcast_removed_dead", count=len(dead))
