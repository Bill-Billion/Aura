"""Schema version contracts shared by scenarios, events, commands, and run artifacts.

The versions in this module describe public, persisted data contracts. A minor
version may add optional fields; a major version requires an explicit migration.
Keeping the constants together prevents a run from reporting a scenario version
while leaving the event and command schemas implicit.
"""

from __future__ import annotations

from dataclasses import dataclass

# ``SUPPORTED_*`` is the newest schema emitted by new benchmark tooling.
# Scenario loading is deliberately multi-major: v1 remains readable so sealed
# historical runs keep resolving to their original contract and fingerprint.
LEGACY_SCENARIO_SCHEMA_VERSION = "1.1"
SUPPORTED_SCENARIO_SCHEMA_VERSION = "2.0"
LATEST_SCENARIO_SCHEMA_VERSION = SUPPORTED_SCENARIO_SCHEMA_VERSION
SUPPORTED_SCENARIO_SCHEMA_BY_MAJOR: dict[int, str] = {
    1: LEGACY_SCENARIO_SCHEMA_VERSION,
    2: SUPPORTED_SCENARIO_SCHEMA_VERSION,
}
SUPPORTED_EVENT_SCHEMA_VERSION = "1.0"
SUPPORTED_COMMAND_SCHEMA_VERSION = "1.0"
SUPPORTED_DEVICE_REGISTRY_VERSION = "1.0"
SUPPORTED_REPORT_SCHEMA_VERSION = "1.0"

SCHEMA_VERSIONS: dict[str, str] = {
    "scenario_schema_version": SUPPORTED_SCENARIO_SCHEMA_VERSION,
    "event_schema_version": SUPPORTED_EVENT_SCHEMA_VERSION,
    "command_schema_version": SUPPORTED_COMMAND_SCHEMA_VERSION,
    "device_registry_version": SUPPORTED_DEVICE_REGISTRY_VERSION,
}


class SchemaVersionError(ValueError):
    """Machine-readable incompatible schema declaration."""

    def __init__(
        self,
        *,
        declared: str,
        supported: str,
        reason: str,
        field: str = "scenario_schema_version",
    ) -> None:
        self.declared = declared
        self.supported = supported
        self.reason = reason
        self.field = field
        super().__init__(
            f"{field}={declared!r} is incompatible with supported {supported!r}: {reason}"
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "field": self.field,
            "declared": self.declared,
            "supported": self.supported,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class VersionCompatibility:
    declared: tuple[int, int]
    supported: tuple[int, int]
    strict: bool
    tolerated: bool
    reason: str


def parse_schema_version(
    raw: object,
    *,
    supported: str = SUPPORTED_SCENARIO_SCHEMA_VERSION,
    field: str = "scenario_schema_version",
) -> tuple[int, int]:
    """Parse ``MAJOR.MINOR`` while tolerating YAML numeric scalars."""

    if isinstance(raw, bool):
        raise SchemaVersionError(
            declared=str(raw),
            supported=supported,
            field=field,
            reason="version must not be boolean",
        )
    if isinstance(raw, (int, float)):
        text = str(raw)
    elif isinstance(raw, str):
        text = raw.strip()
    else:
        raise SchemaVersionError(
            declared=str(raw),
            supported=supported,
            field=field,
            reason=f"unsupported version type: {type(raw).__name__}",
        )

    parts = text.split(".")
    if len(parts) != 2 or any(not part.isdigit() for part in parts):
        raise SchemaVersionError(
            declared=text,
            supported=supported,
            field=field,
            reason="version must have exactly the form MAJOR.MINOR (for example '1.0')",
        )
    try:
        major = int(parts[0])
        minor = int(parts[1])
    except (ValueError, IndexError):
        raise SchemaVersionError(
            declared=text,
            supported=supported,
            field=field,
            reason="version must have the form MAJOR.MINOR (for example '1.0')",
        ) from None
    if major < 0 or minor < 0:
        raise SchemaVersionError(
            declared=text,
            supported=supported,
            field=field,
            reason="version components must be non-negative",
        )
    return major, minor


def check_schema_compatibility(
    declared: object,
    supported: str = SUPPORTED_SCENARIO_SCHEMA_VERSION,
    *,
    field: str = "scenario_schema_version",
) -> VersionCompatibility:
    """Apply the §14 major/minor compatibility rules."""

    declared_text = str(declared).strip()
    declared_pair = parse_schema_version(declared, supported=supported, field=field)
    supported_pair = parse_schema_version(supported, supported=supported, field=field)

    if declared_pair[0] != supported_pair[0]:
        raise SchemaVersionError(
            declared=declared_text,
            supported=supported,
            field=field,
            reason=(
                f"unknown major version {declared_pair[0]} (supported major is "
                f"{supported_pair[0]}); an explicit migration is required"
            ),
        )

    if declared_pair[1] > supported_pair[1]:
        return VersionCompatibility(
            declared=declared_pair,
            supported=supported_pair,
            strict=False,
            tolerated=True,
            reason=(
                f"declared minor {declared_pair[1]} is newer than supported "
                f"{supported_pair[1]}; unknown optional fields may be ignored"
            ),
        )

    return VersionCompatibility(
        declared=declared_pair,
        supported=supported_pair,
        strict=True,
        tolerated=False,
        reason="declared minor is supported; validate strictly",
    )


def check_scenario_schema_compatibility(declared: object) -> VersionCompatibility:
    """Dispatch ScenarioSpec compatibility by declared major version.

    Generic event/command/report schemas still support exactly one major and
    use :func:`check_schema_compatibility` directly.  Scenarios are different:
    AuraBench 2.0 is additive at runtime, while persisted v1 scenarios must
    remain loadable for historical run evaluation.
    """

    declared_pair = parse_schema_version(
        declared,
        supported=SUPPORTED_SCENARIO_SCHEMA_VERSION,
        field="scenario_schema_version",
    )
    supported = SUPPORTED_SCENARIO_SCHEMA_BY_MAJOR.get(declared_pair[0])
    if supported is None:
        raise SchemaVersionError(
            declared=str(declared).strip(),
            supported=SUPPORTED_SCENARIO_SCHEMA_VERSION,
            field="scenario_schema_version",
            reason=(
                f"unknown major version {declared_pair[0]} (supported majors are "
                f"{sorted(SUPPORTED_SCENARIO_SCHEMA_BY_MAJOR)}); an explicit migration "
                "is required"
            ),
        )
    return check_schema_compatibility(
        declared,
        supported=supported,
        field="scenario_schema_version",
    )


__all__ = [
    "LATEST_SCENARIO_SCHEMA_VERSION",
    "LEGACY_SCENARIO_SCHEMA_VERSION",
    "SCHEMA_VERSIONS",
    "SUPPORTED_SCENARIO_SCHEMA_BY_MAJOR",
    "SUPPORTED_COMMAND_SCHEMA_VERSION",
    "SUPPORTED_DEVICE_REGISTRY_VERSION",
    "SUPPORTED_EVENT_SCHEMA_VERSION",
    "SUPPORTED_REPORT_SCHEMA_VERSION",
    "SUPPORTED_SCENARIO_SCHEMA_VERSION",
    "SchemaVersionError",
    "VersionCompatibility",
    "check_scenario_schema_compatibility",
    "check_schema_compatibility",
    "parse_schema_version",
]
