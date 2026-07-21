"""S1-T7 最小设备效果模型：fan 体感 / camera 安防覆盖 / sensor 读数（spec §3.4）。

三条最小效果，不是完整物理重做：

- fan：power=true 且 speed 分档降**体感**温度，shake 放大房间级覆盖系数，
  绝不动 ``room.temperature`` 这一物理 ground truth（§3.4 明文）。
- camera：按房间统计 online 摄像头占比归一为 coverage∈[0,1]，offline 即掉覆盖。
- sensor：读数由 ground truth 派生而非手写（§2.3 ground truth / observable 分离的
  最小落点），签名带 rng/delay/noise 给 S2 seed 化留缝，本阶段默认确定性直传。

§3.4 末段要求每条效果显式声明作用域（physical / perceived_comfort /
security_coverage / ui_observability），故效果函数集中登记在 EFFECT_MODELS。
"""

import random

import pytest

from backend.config.capability_matrix import EffectClass
from backend.engine.state import (
    DeviceState,
    DeviceStateValues,
    Location3D,
    RoomState,
    WorldState,
)
from backend.engine.state_manager import StateManager
from backend.execution.command import CommandSource, CommandStatus, DeviceCommand
from backend.execution.executor import FEEDBACK_EVENT_TYPE, CommandExecutor
from backend.simulators.effects import (
    EFFECT_MODELS,
    FAN_SHAKE_COVERAGE_FACTOR,
    FAN_SPEED_PERCEIVED_OFFSET,
    calculate_perceived_temperature,
    calculate_security_coverage,
    derive_observable_updates,
    generate_sensor_reading,
)
from backend.simulators.environment import EnvironmentSimulator


def _fan(device_id: str, room: str, *, power: bool, speed: str = "low", shake: bool = False):
    return DeviceState(
        id=device_id,
        type="fan",
        location=Location3D(room=room),
        capabilities=["power", "speed", "shake", "timeout"],
        state=DeviceStateValues(
            power=power, extra={"speed": speed, "shake": shake, "timeout": 0}
        ),
    )


def _camera(device_id: str, room: str, *, online: bool = True):
    return DeviceState(
        id=device_id,
        type="camera",
        location=Location3D(room=room),
        capabilities=["view"],
        state=DeviceStateValues(power=True, extra={"online": online}),
    )


def _sensor(device_id: str, room: str, *, sensor_type: str = "temperature", value=24.5):
    return DeviceState(
        id=device_id,
        type="sensor",
        location=Location3D(room=room),
        capabilities=["read"],
        state=DeviceStateValues(
            power=True, extra={"sensor_type": sensor_type, "value": value, "unit": "°C"}
        ),
    )


def _make_world(*devices, room_temp: float = 28.0) -> WorldState:
    world = WorldState()
    world.rooms = {"living_room": RoomState(id="living_room", temperature=room_temp)}
    world.devices = {device.id: device for device in devices}
    return world


def _collector():
    events = []

    async def publish(event):
        events.append(event)
        return event

    return events, publish


# ----------------------------------------------------------------------
# fan：体感温度
# ----------------------------------------------------------------------


def test_fan_high_reduces_perceived_temp_but_physical_temp_unchanged():
    world = _make_world(_fan("fan_1", "living_room", power=True, speed="high"), room_temp=28.0)
    room = world.rooms["living_room"]

    perceived = calculate_perceived_temperature(room, world)

    assert perceived == pytest.approx(28.0 + FAN_SPEED_PERCEIVED_OFFSET["high"])
    assert perceived < room.temperature
    # §3.4：风扇绝不改物理温度这一 ground truth
    assert room.temperature == 28.0


def test_fan_speed_tiers_are_monotonic():
    room_temp = 28.0
    perceived = []
    for speed in ("low", "medium", "high"):
        world = _make_world(
            _fan("fan_1", "living_room", power=True, speed=speed), room_temp=room_temp
        )
        perceived.append(calculate_perceived_temperature(world.rooms["living_room"], world))

    assert perceived[0] > perceived[1] > perceived[2]


def test_fan_shake_widens_room_comfort_coverage():
    """shake=true 提高房间级覆盖系数 → 同档位体感降幅更大。"""

    world_still = _make_world(
        _fan("fan_1", "living_room", power=True, speed="medium"), room_temp=28.0
    )
    world_shake = _make_world(
        _fan("fan_1", "living_room", power=True, speed="medium", shake=True), room_temp=28.0
    )

    still = calculate_perceived_temperature(world_still.rooms["living_room"], world_still)
    shaking = calculate_perceived_temperature(world_shake.rooms["living_room"], world_shake)

    assert shaking < still
    assert shaking == pytest.approx(
        round(28.0 + FAN_SPEED_PERCEIVED_OFFSET["medium"] * FAN_SHAKE_COVERAGE_FACTOR, 2)
    )


def test_fan_off_removes_perceived_delta():
    world = _make_world(
        _fan("fan_1", "living_room", power=False, speed="high"), room_temp=28.0
    )
    room = world.rooms["living_room"]

    assert calculate_perceived_temperature(room, world) == pytest.approx(room.temperature)


@pytest.mark.anyio
async def test_fan_command_emits_perceived_temperature_feedback():
    """executor 默认 effect 钩子：fan 命令成功后重算体感并发 feedback.state_delta。"""

    events, publish = _collector()
    sm = StateManager(_make_world(_fan("fan_1", "living_room", power=False), room_temp=28.0))
    ex = CommandExecutor(sm, publish)

    record = await ex.submit(
        DeviceCommand(
            source=CommandSource.UI, device_id="fan_1", capability="power", value=True
        )
    )

    assert record.status == CommandStatus.SUCCEEDED
    paths = {
        event.data["path"] for event in events if event.event_type == FEEDBACK_EVENT_TYPE
    }
    assert "rooms[living_room].perceived_temperature" in paths
    assert sm.world.rooms["living_room"].perceived_temperature == pytest.approx(
        28.0 + FAN_SPEED_PERCEIVED_OFFSET["low"]
    )
    # 物理温度纹丝不动
    assert sm.world.rooms["living_room"].temperature == 28.0


@pytest.mark.anyio
async def test_light_command_does_not_write_perceived_temperature():
    """非 fan/camera 命令不应触发这两条派生效果（避免噪声 delta）。"""

    events, publish = _collector()
    world = _make_world(room_temp=28.0)
    world.devices["light_1"] = DeviceState(
        id="light_1",
        type="light",
        location=Location3D(room="living_room"),
        capabilities=["power", "brightness"],
        state=DeviceStateValues(power=False, extra={"brightness": 50}),
    )
    sm = StateManager(world)
    ex = CommandExecutor(sm, publish)

    await ex.submit(
        DeviceCommand(
            source=CommandSource.UI, device_id="light_1", capability="power", value=True
        )
    )

    paths = {
        event.data["path"] for event in events if event.event_type == FEEDBACK_EVENT_TYPE
    }
    assert "rooms[living_room].light_level" in paths
    assert "rooms[living_room].perceived_temperature" not in paths
    assert sm.world.rooms["living_room"].perceived_temperature is None


# ----------------------------------------------------------------------
# camera：安防覆盖
# ----------------------------------------------------------------------


def test_camera_offline_reduces_room_security_coverage():
    world = _make_world(
        _camera("cam_a", "living_room", online=True),
        _camera("cam_b", "living_room", online=True),
    )

    assert calculate_security_coverage(world)["living_room"] == pytest.approx(1.0)

    world.devices["cam_b"].state.extra["online"] = False

    coverage = calculate_security_coverage(world)
    assert coverage["living_room"] == pytest.approx(0.5)
    assert 0.0 <= coverage["living_room"] <= 1.0


def test_room_without_camera_has_zero_coverage():
    world = _make_world(_fan("fan_1", "living_room", power=False))

    assert calculate_security_coverage(world)["living_room"] == 0.0


# ----------------------------------------------------------------------
# sensor：读数由 ground truth 派生
# ----------------------------------------------------------------------


def test_sensor_reading_derived_from_ground_truth_deterministic_by_default():
    assert generate_sensor_reading(24.5) == 24.5
    # 默认无噪声：同一 ground truth 重复调用恒等
    assert [generate_sensor_reading(21.25) for _ in range(5)] == [21.25] * 5


def test_sensor_reading_noise_requires_rng_and_is_seed_reproducible():
    a = generate_sensor_reading(24.5, rng=random.Random(7), noise=0.5)
    b = generate_sensor_reading(24.5, rng=random.Random(7), noise=0.5)

    assert a == b
    assert a != 24.5
    # 未注入 rng 时即使给了 noise 也保持确定性（本阶段不引入隐式全局随机源）
    assert generate_sensor_reading(24.5, noise=0.5) == 24.5


def test_sensor_reading_delay_holds_previous_value():
    assert generate_sensor_reading(26.0, delay=1, previous_value=24.5) == 24.5
    assert generate_sensor_reading(26.0, delay=0, previous_value=24.5) == 26.0


def test_environment_step_refreshes_sensor_reading_from_ground_truth():
    world = _make_world(_sensor("sensor_1", "living_room"), room_temp=28.0)
    updates = EnvironmentSimulator().step(world, dt=1.0)

    next_temp = updates["rooms[living_room].temperature"]
    assert updates["devices[sensor_1].state.extra.value"] == pytest.approx(next_temp)
    # 手写的旧读数被 ground truth 覆盖，不再是硬编码
    assert world.devices["sensor_1"].state.extra["value"] == 24.5


def test_sensor_without_ground_truth_source_is_left_untouched():
    world = _make_world(
        _sensor("sensor_air", "living_room", sensor_type="air_quality", value=42)
    )

    updates = derive_observable_updates(world)

    assert "devices[sensor_air].state.extra.value" not in updates


def test_derive_observable_updates_covers_perceived_and_coverage():
    world = _make_world(
        _fan("fan_1", "living_room", power=True, speed="high"),
        _camera("cam_a", "living_room", online=False),
        room_temp=28.0,
    )

    updates = derive_observable_updates(world)

    assert updates["rooms[living_room].perceived_temperature"] == pytest.approx(
        28.0 + FAN_SPEED_PERCEIVED_OFFSET["high"]
    )
    assert updates["rooms[living_room].security_coverage"] == 0.0
    # 派生量不得混进物理量
    assert "rooms[living_room].temperature" not in updates


# ----------------------------------------------------------------------
# §3.4 效果声明
# ----------------------------------------------------------------------


def test_every_effect_declares_effect_class():
    assert EFFECT_MODELS, "至少三条最小效果必须登记"

    allowed = set(EffectClass.__args__)
    for name, fn in EFFECT_MODELS.items():
        assert getattr(fn, "effect_class", None) in allowed, name

    assert EFFECT_MODELS["calculate_perceived_temperature"].effect_class == "perceived_comfort"
    assert EFFECT_MODELS["calculate_security_coverage"].effect_class == "security_coverage"
    assert EFFECT_MODELS["generate_sensor_reading"].effect_class == "ui_observability"
