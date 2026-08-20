"""Backward-compatible import path for the shared schema-version contract."""

from backend.models.versioning import (
    SUPPORTED_SCENARIO_SCHEMA_VERSION,
    SchemaVersionError,
    VersionCompatibility,
    check_schema_compatibility,
    parse_schema_version,
)

__all__ = [
    "SUPPORTED_SCENARIO_SCHEMA_VERSION",
    "SchemaVersionError",
    "VersionCompatibility",
    "check_schema_compatibility",
    "parse_schema_version",
]
