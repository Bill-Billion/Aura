from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter

router = APIRouter()
_health_provider: Callable[[], dict[str, Any]] | None = None


def configure_health_provider(provider: Callable[[], dict[str, Any]]) -> None:
    global _health_provider
    _health_provider = provider


@router.get("/api/scenes")
async def get_scenes():
    return {
        "scenes": [
            {"id": "apartment_v1", "name": "测试公寓"},
        ]
    }


@router.get("/api/health")
async def health_check():
    if _health_provider is None:
        return {"status": "ok"}
    return _health_provider()
