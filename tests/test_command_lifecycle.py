import pytest

from backend.engine.event_bus import SimEvent
from backend.execution.command import (
    LEGAL_TRANSITIONS,
    LIFECYCLE_EVENT_TYPE,
    TERMINAL_STATES,
    CommandRecord,
    CommandSource,
    CommandStatus,
    DeviceCommand,
    IllegalTransitionError,
)


def _make_command(**overrides) -> DeviceCommand:
    params = dict(
        device_id="light-001",
        capability="power",
        value=True,
        source=CommandSource.UI,
        correlation_id="corr-1",
        causal_parent="root-1",
    )
    params.update(overrides)
    return DeviceCommand(**params)


def _collector():
    events: list[SimEvent] = []

    async def publish(event: SimEvent) -> SimEvent:
        events.append(event)
        return event

    return events, publish


def test_enum_values_match_spec_10_1_and_source_contract():
    assert {s.value for s in CommandStatus} == {
        "proposed",
        "approved",
        "rejected",
        "validated",
        "executing",
        "succeeded",
        "failed",
        "timed_out",
        "cancelled",
        "superseded",
    }
    assert {s.value for s in CommandSource} == {
        "ui",
        "agent",
        "scenario",
        "rule_fallback",
    }
    assert TERMINAL_STATES == frozenset(
        {
            CommandStatus.REJECTED,
            CommandStatus.SUCCEEDED,
            CommandStatus.FAILED,
            CommandStatus.TIMED_OUT,
            CommandStatus.CANCELLED,
            CommandStatus.SUPERSEDED,
        }
    )
    # 终态吸收：合法迁移表里终态出边为空
    for terminal in TERMINAL_STATES:
        assert LEGAL_TRANSITIONS[terminal] == frozenset()


@pytest.mark.anyio
async def test_happy_path_proposed_approved_validated_executing_succeeded_emits_5_events():
    events, publish = _collector()
    cmd = _make_command()

    rec = await CommandRecord.propose(cmd, publish_event=publish)
    await rec.transition(CommandStatus.APPROVED)
    await rec.transition(CommandStatus.VALIDATED)
    await rec.transition(CommandStatus.EXECUTING)
    await rec.transition(CommandStatus.SUCCEEDED)

    assert len(events) == 5
    assert all(e.event_type == LIFECYCLE_EVENT_TYPE for e in events)
    assert [e.data["to_status"] for e in events] == [
        "proposed",
        "approved",
        "validated",
        "executing",
        "succeeded",
    ]
    # 出生事件 from_status 为 None
    assert events[0].data["from_status"] is None
    assert events[1].data["from_status"] == "proposed"
    assert rec.status == CommandStatus.SUCCEEDED
    assert rec.is_terminal


@pytest.mark.anyio
async def test_illegal_transition_raises():
    events, publish = _collector()

    # succeeded -> executing 非法（终态）
    rec = await CommandRecord.propose(_make_command(), publish_event=publish)
    await rec.transition(CommandStatus.APPROVED)
    await rec.transition(CommandStatus.VALIDATED)
    await rec.transition(CommandStatus.EXECUTING)
    await rec.transition(CommandStatus.SUCCEEDED)
    with pytest.raises(IllegalTransitionError):
        await rec.transition(CommandStatus.EXECUTING)

    # proposed -> executing 跳级非法
    rec2 = await CommandRecord.propose(_make_command(), publish_event=publish)
    count_before = len(events)
    with pytest.raises(IllegalTransitionError):
        await rec2.transition(CommandStatus.EXECUTING)
    # 非法迁移不发事件、不改状态
    assert len(events) == count_before
    assert rec2.status == CommandStatus.PROPOSED


@pytest.mark.anyio
async def test_terminal_states_are_absorbing():
    events, publish = _collector()

    async def drive_to(target: CommandStatus) -> CommandRecord:
        rec = await CommandRecord.propose(_make_command(), publish_event=publish)
        if target == CommandStatus.CANCELLED:
            await rec.transition(CommandStatus.CANCELLED)
        elif target == CommandStatus.SUPERSEDED:
            await rec.transition(CommandStatus.SUPERSEDED)
        elif target == CommandStatus.FAILED:
            await rec.transition(CommandStatus.APPROVED)
            await rec.transition(CommandStatus.FAILED, failure="invalid_value_range")
        elif target == CommandStatus.TIMED_OUT:
            await rec.transition(CommandStatus.APPROVED)
            await rec.transition(CommandStatus.VALIDATED)
            await rec.transition(CommandStatus.EXECUTING)
            await rec.transition(CommandStatus.TIMED_OUT, failure="execution_timeout")
        else:
            raise AssertionError(target)
        return rec

    for terminal in (
        CommandStatus.FAILED,
        CommandStatus.TIMED_OUT,
        CommandStatus.CANCELLED,
        CommandStatus.SUPERSEDED,
    ):
        rec = await drive_to(terminal)
        assert rec.status == terminal
        assert rec.is_terminal
        for nxt in CommandStatus:
            with pytest.raises(IllegalTransitionError):
                await rec.transition(nxt)


@pytest.mark.anyio
async def test_lifecycle_events_inherit_correlation_and_causal_parent():
    events, publish = _collector()
    cmd = _make_command(correlation_id="corr-xyz", causal_parent="root-abc")

    rec = await CommandRecord.propose(cmd, publish_event=publish)
    await rec.transition(CommandStatus.APPROVED)
    await rec.transition(CommandStatus.VALIDATED)

    for e in events:
        assert e.event_type == LIFECYCLE_EVENT_TYPE
        assert e.correlation_id == "corr-xyz"
        assert e.causal_parent == "root-abc"
        assert e.data["command_id"] == cmd.command_id
        assert e.data["source"] == "ui"


@pytest.mark.anyio
async def test_transition_failure_code_recorded_in_event_and_record():
    events, publish = _collector()
    rec = await CommandRecord.propose(_make_command(), publish_event=publish)
    await rec.transition(CommandStatus.APPROVED)
    await rec.transition(CommandStatus.FAILED, failure="capability_not_supported")

    assert events[-1].data["to_status"] == "failed"
    assert events[-1].data["failure_code"] == "capability_not_supported"
    assert rec.failure_code == "capability_not_supported"
    # 未失败的迁移事件不带 failure_code 键
    assert "failure_code" not in events[0].data


@pytest.mark.anyio
async def test_transition_is_representable_as_event_payload_without_bus():
    # publish_event 缺省时仍可构造事件负载（迁移即事件的纯数据形态）
    rec = CommandRecord(_make_command(), status=CommandStatus.PROPOSED)
    event = rec.build_lifecycle_event(
        from_status=CommandStatus.PROPOSED,
        to_status=CommandStatus.APPROVED,
    )
    assert isinstance(event, SimEvent)
    assert event.event_type == LIFECYCLE_EVENT_TYPE
    assert event.data["from_status"] == "proposed"
    assert event.data["to_status"] == "approved"
    assert event.data["source"] == "ui"
