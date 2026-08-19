"""Network-boundary controls for research-run launch requests.

Rule-based and mocked runs are safe to expose to a configured research UI.  A
live run (including the first capture of a recorded baseline) is different: it
can spend a server-owned API key.  That capability is therefore opt-in, local
by default, and token-gated when used from a remote client.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import hmac
import ipaddress
import os
import re
from urllib.parse import urlsplit

from backend.agents.llm_modes import ALLOW_LIVE_LLM_ENV, live_llm_allowed
from backend.models.schemas import BaselinePolicy, RunScenarioPayload


ALLOWED_ORIGINS_ENV = "AURA_ALLOWED_ORIGINS"
RESEARCH_WRITE_TOKEN_ENV = "AURA_RESEARCH_WRITE_TOKEN"

# Local UI/dev-server origins are trusted without maintaining a brittle port
# list.  Remote origins must be listed explicitly in AURA_ALLOWED_ORIGINS.
LOCAL_ORIGIN_REGEX = r"^https?://(?:localhost|127\.0\.0\.1|\[::1\])(?::\d+)?$"
_LOCAL_ORIGIN_RE = re.compile(LOCAL_ORIGIN_REGEX, re.IGNORECASE)


@dataclass(slots=True)
class ResearchAccessError(Exception):
    status_code: int
    code: str
    message: str
    details: dict[str, object] = field(default_factory=dict)


def configured_allowed_origins(
    env: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Return exact remote origins; wildcard entries are rejected fail-closed."""

    environ = os.environ if env is None else env
    raw = str(environ.get(ALLOWED_ORIGINS_ENV, ""))
    origins: list[str] = []
    for item in raw.split(","):
        origin = item.strip().rstrip("/")
        if not origin:
            continue
        parsed = urlsplit(origin)
        if (
            origin == "*"
            or parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                f"{ALLOWED_ORIGINS_ENV} contains invalid origin {origin!r}; "
                "use comma-separated absolute http(s) origins, never '*'"
            )
        if origin not in origins:
            origins.append(origin)
    return tuple(origins)


def origin_is_trusted(
    origin: str | None,
    env: Mapping[str, str] | None = None,
) -> bool:
    """A missing Origin is a non-browser client; present origins are strict."""

    if origin is None:
        return True
    normalized = origin.strip().rstrip("/")
    if not normalized or normalized == "null":
        return False
    if _LOCAL_ORIGIN_RE.fullmatch(normalized):
        return True
    return normalized in configured_allowed_origins(env)


def is_loopback_client(host: str | None) -> bool:
    if not host:
        return False
    normalized = host.strip().strip("[]").lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def request_targets_loopback(headers: Mapping[str, str]) -> bool:
    """Return whether the HTTP Host itself names a loopback endpoint."""

    raw_host = str(headers.get("host", "")).strip()
    if not raw_host:
        return False
    try:
        hostname = urlsplit(f"//{raw_host}").hostname
    except ValueError:
        return False
    return is_loopback_client(hostname)


def launch_can_spend_server_credentials(payload: RunScenarioPayload) -> bool:
    """Only live and first-capture recorded policies can issue paid requests."""

    return payload.baseline_policy is BaselinePolicy.LLM_LIVE or (
        payload.baseline_policy is BaselinePolicy.LLM_RECORDED
        and payload.recording_source_run_id is None
    )


def _bearer_token(headers: Mapping[str, str]) -> str | None:
    value = str(headers.get("authorization", "")).strip()
    scheme, separator, token = value.partition(" ")
    if not separator or scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def authorize_run_launch(
    payload: RunScenarioPayload,
    *,
    headers: Mapping[str, str],
    client_host: str | None,
    env: Mapping[str, str] | None = None,
) -> None:
    """Authorize a REST/WS launch without accepting credentials in its payload."""

    environ = os.environ if env is None else env
    origin = headers.get("origin")
    if not origin_is_trusted(origin, environ):
        raise ResearchAccessError(
            403,
            "origin_not_allowed",
            "请求 Origin 未获准访问仿真控制面",
            {"origin": str(origin), "allowed_origins_env": ALLOWED_ORIGINS_ENV},
        )

    if not launch_can_spend_server_credentials(payload):
        return

    if not live_llm_allowed(environ):
        raise ResearchAccessError(
            503,
            "baseline_policy_unavailable",
            "付费 LLM 策略未由服务端显式启用",
            {
                "baseline_policy": payload.baseline_policy.value,
                "reason_code": "live_llm_disabled",
                "required_env": ALLOW_LIVE_LLM_ENV,
            },
        )

    # Explicit opt-in + a loopback transport is the default local trust
    # boundary.  Remote deployments must additionally prove possession of a
    # server-configured bearer token; an allowed browser Origin alone is not
    # authorization to spend the server's key.
    local_origin = origin is not None and _LOCAL_ORIGIN_RE.fullmatch(
        origin.strip().rstrip("/")
    )
    local_cli = origin is None and request_targets_loopback(headers)
    if is_loopback_client(client_host) and (local_origin or local_cli):
        return

    expected = str(environ.get(RESEARCH_WRITE_TOKEN_ENV, "")).strip()
    supplied = _bearer_token(headers)
    if not expected or supplied is None or not hmac.compare_digest(supplied, expected):
        raise ResearchAccessError(
            403,
            "research_write_unauthorized",
            "远程付费研究运行需要有效的 Bearer token",
            {
                "baseline_policy": payload.baseline_policy.value,
                "required_env": RESEARCH_WRITE_TOKEN_ENV,
            },
        )
