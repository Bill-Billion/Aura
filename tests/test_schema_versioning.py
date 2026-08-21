import pytest

from backend.config.device_registry import DEVICE_REGISTRY_VERSION
from backend.engine.event_bus import SimEvent
from backend.engine.run_manager import RunManager
from backend.engine.state import WorldState
from backend.execution.command import (
    CommandRecord,
    CommandSource,
    CommandStatus,
    DeviceCommand,
)
from backend.models.versioning import (
    SCHEMA_VERSIONS,
    SUPPORTED_COMMAND_SCHEMA_VERSION,
    SUPPORTED_DEVICE_REGISTRY_VERSION,
    SUPPORTED_EVENT_SCHEMA_VERSION,
    SUPPORTED_SCENARIO_SCHEMA_VERSION,
    SchemaVersionError,
    check_schema_compatibility,
)


def test_shared_version_contract_is_centralized() -> None:
    assert SCHEMA_VERSIONS == {
        "scenario_schema_version": SUPPORTED_SCENARIO_SCHEMA_VERSION,
        "event_schema_version": SUPPORTED_EVENT_SCHEMA_VERSION,
        "command_schema_version": SUPPORTED_COMMAND_SCHEMA_VERSION,
        "device_registry_version": SUPPORTED_DEVICE_REGISTRY_VERSION,
    }
    assert DEVICE_REGISTRY_VERSION == SUPPORTED_DEVICE_REGISTRY_VERSION


def test_schema_compatibility_reports_the_requested_field() -> None:
    compatibility = check_schema_compatibility(
        "1.1",
        supported="1.0",
        field="event_schema_version",
    )
    assert compatibility.tolerated is True
    assert compatibility.strict is False


@pytest.mark.parametrize("declared", ["1", "1.0.999", "1.x", "1.", ".1", " 1.0.0 "])
def test_schema_version_requires_exact_major_minor_shape(declared: str) -> None:
    with pytest.raises(SchemaVersionError) as excinfo:
        check_schema_compatibility(declared)

    assert excinfo.value.declared == declared.strip()
    assert "MAJOR.MINOR" in excinfo.value.reason


def test_sim_event_carries_event_schema_version() -> None:
    event = SimEvent(event_type="test.event", source="test", timestamp=0.0)
    assert event.event_schema_version == SUPPORTED_EVENT_SCHEMA_VERSION
    assert event.model_dump()["event_schema_version"] == SUPPORTED_EVENT_SCHEMA_VERSION


def test_command_and_lifecycle_carry_command_schema_version() -> None:
    command = DeviceCommand(
        source=CommandSource.AGENT,
        device_id="light_living_01",
        capability="power",
        value=True,
    )
    assert command.command_schema_version == SUPPORTED_COMMAND_SCHEMA_VERSION

    lifecycle = CommandRecord(command).build_lifecycle_event(
        from_status=None,
        to_status=CommandStatus.PROPOSED,
    )
    assert lifecycle.data["command_schema_version"] == SUPPORTED_COMMAND_SCHEMA_VERSION


def test_new_run_metadata_records_all_four_schema_versions() -> None:
    metadata = RunManager().start_run(world=WorldState(scene_id="version-test"), seed=0)
    assert {
        "scenario_schema_version": metadata.scenario_schema_version,
        "event_schema_version": metadata.event_schema_version,
        "command_schema_version": metadata.command_schema_version,
        "device_registry_version": metadata.device_registry_version,
    } == SCHEMA_VERSIONS
