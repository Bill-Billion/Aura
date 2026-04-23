from __future__ import annotations

import os
from pathlib import Path


LOCAL_ENV_FILES = (".env.local", ".env")


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def load_local_env() -> dict[str, str]:
    """从仓库根目录加载本地环境变量文件，但不覆盖已有环境变量。"""
    loaded: dict[str, str] = {}

    for filename in LOCAL_ENV_FILES:
        env_path = repo_root() / filename
        if not env_path.exists():
            continue

        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue

            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip("'\"")
            if not key or key in os.environ:
                continue

            os.environ[key] = value
            loaded[key] = value

    return loaded
