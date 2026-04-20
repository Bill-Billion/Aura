#!/usr/bin/env python3
"""验证 WebSocket 连接能收到首帧并稳定保持一段时间。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

import websockets


async def verify_ws(url: str, hold_seconds: float) -> int:
    try:
        async with websockets.connect(url) as ws:
            raw = await asyncio.wait_for(ws.recv(), timeout=5)
            payload = json.loads(raw)
            if payload.get("type") != "STATE_FULL":
                print(f"[ws-check] 首帧类型不对: {payload.get('type')}", file=sys.stderr)
                return 1

            await asyncio.sleep(hold_seconds)
            if ws.state.name != "OPEN":
                print(f"[ws-check] 连接在保活窗口内关闭: state={ws.state.name}", file=sys.stderr)
                return 1
    except Exception as exc:  # pragma: no cover - CLI diagnostics
        print(f"[ws-check] 校验失败: {exc}", file=sys.stderr)
        return 1

    print(f"[ws-check] {url} 已收到 STATE_FULL，且保持 {hold_seconds:.1f}s 不断线")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="待验证的 websocket 地址")
    parser.add_argument(
        "--hold-seconds",
        type=float,
        default=5.0,
        help="收到首帧后保持连接的秒数",
    )
    args = parser.parse_args()
    return asyncio.run(verify_ws(args.url, args.hold_seconds))


if __name__ == "__main__":
    raise SystemExit(main())
