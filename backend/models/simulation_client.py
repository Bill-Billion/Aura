from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from backend.models.schemas import SimCommand, WSMessage


@runtime_checkable
class SimulationClient(Protocol):
    """Phase 1 对外暴露的最小仿真客户端协议。"""

    async def connect(self) -> None:
        """建立与仿真服务的连接。"""

    async def disconnect(self) -> None:
        """断开当前连接并释放资源。"""

    async def send_command(self, command: SimCommand) -> None:
        """发送一条结构化仿真命令。"""

    async def iter_events(self) -> AsyncIterator[WSMessage]:
        """持续产出服务端消息流。"""

    async def get_snapshot(self) -> dict[str, object]:
        """获取当前世界状态快照。"""
