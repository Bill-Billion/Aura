"""§7 Event-To-Device Mapping 落在 agent 相关性上（S2 review：episode 爆炸收口）。

S2-T6 把 ``subscribed_event_types`` 从三个旧事件放宽到 §4.1 全部十七个根事件，
但 :meth:`BaseAgent.is_relevant` 的兜底分支仍是 ``return True``——于是每个注册 agent
对每一条 ``device.offline`` / ``security.*`` / ``safety.*`` 都开一条 episode：
照明 agent 会为"一台摄像头掉线"跑一轮推理。S3 还要再挂三个 domain agent 到同一个
扇出上，episode 数量按 agent 数线性膨胀。

spec §7 原文给出的收口方式是数据而不是分支：*Every root event type must declare its
default device relevance ... The implementation should expose this mapping as data,
not hard-coded branching inside individual agents.* 本测试因此断两件事：

1. **收紧**：agent 只对"它控的设备类型能回应的事件"开 episode；
2. **不误伤**：环境去抖与 user.command 设备过滤的既有语义逐条保持。
"""

from __future__ import annotations

import pytest

from backend.agents.hvac import HVACAgent
from backend.agents.lighting import LightingAgent
from backend.agents.llm import LLMProvider, LLMProviderError
from backend.agents.relevance import actionable_device_types
from backend.api.ws import ConnectionManager
from backend.engine.event_bus import EventBus, SimEvent
from backend.engine.event_types import ALL_ROOT_EVENT_TYPES
from backend.engine.simulation import SimulationEngine
from backend.main import _init_default_state


@pytest.fixture
def world():
    return _init_default_state().world


def _event(event_type: str, **data) -> SimEvent:
    return SimEvent(event_type=event_type, source="test", timestamp=1.0, data=dict(data))


class _DisabledProvider(LLMProvider):
    """一律失败 → agent 走规则回退；本测试只关心"有没有 episode"。"""

    provider_name = "disabled"
    model = "rule_based"

    async def generate_decision(self, request):  # type: ignore[override]
        raise LLMProviderError("provider_error", "LLM provider is disabled")


# --------------------------------------------------------------- 1. 设备族匹配


def test_lighting_agent_does_not_open_an_episode_for_an_offline_camera(world):
    """照明 agent 控的是灯；一台摄像头掉线它一条命令也发不出来。"""

    offline = _event(
        "device.offline",
        device_id="camera_living_02",
        device_type="camera",
        online=False,
    )
    assert LightingAgent().is_relevant(world, offline) is False
    assert HVACAgent().is_relevant(world, offline) is False


def test_hvac_agent_still_opens_an_episode_for_its_own_offline_device(world):
    """§7「device.offline → affected device | alternatives」：控着同类设备的 agent 才有戏。"""

    offline = _event(
        "device.offline",
        device_id="ac_living_01",
        device_type="hvac",
        online=False,
    )
    assert HVACAgent().is_relevant(world, offline) is True
    assert LightingAgent().is_relevant(world, offline) is False


def test_device_recovered_uses_the_same_device_family_rule(world):
    recovered = _event("device.recovered", device_id="light_living_01", online=True)
    assert LightingAgent().is_relevant(world, recovered) is True
    assert HVACAgent().is_relevant(world, recovered) is False


def test_device_type_falls_back_to_event_data_when_the_device_is_unknown(world):
    """设备不在世界里（场景外注入 / 已被移除）时按事件自带的 device_type 判定。"""

    offline = _event("device.offline", device_id="ac_ghost_99", device_type="hvac")
    assert HVACAgent().is_relevant(world, offline) is True
    assert LightingAgent().is_relevant(world, offline) is False


def test_unidentifiable_device_availability_event_relates_to_nobody(world):
    """连是哪台设备都说不出的可用性事件，没有 agent 能定位替代品。"""

    offline = _event("device.offline")
    assert LightingAgent().is_relevant(world, offline) is False
    assert HVACAgent().is_relevant(world, offline) is False


# --------------------------------------------------------------- 2. 安防 / 安全


def test_security_events_reach_lighting_but_not_hvac(world):
    """§7「security.presence_detected → cameras, entry lights | sensors」。"""

    for event_type in ("security.presence_detected", "security.door_opened"):
        event = _event(event_type, room_id="living_room")
        assert LightingAgent().is_relevant(world, event) is True, event_type
        assert HVACAgent().is_relevant(world, event) is False, event_type


def test_smoke_stays_relevant_to_everyone(world):
    """§19「prefer fail-closed behavior for safety and security events」：烟雾谁都要管。"""

    smoke = _event("safety.smoke_detected", room_id="kitchen")
    assert LightingAgent().is_relevant(world, smoke) is True
    assert HVACAgent().is_relevant(world, smoke) is True


# --------------------------------------------------------------- 3. 既有语义不变


def test_environment_debounce_still_requires_significant_change_reasons(world):
    """环境类根事件仍然是"没有显著变化理由就不开 episode"。"""

    quiet = _event("environment.temperature_threshold", room_id="living_room", value=31.0)
    assert HVACAgent().is_relevant(world, quiet) is False

    loud = _event(
        "environment.temperature_threshold",
        room_id="living_room",
        value=31.0,
        significant_change_reasons=["room_temperature_threshold"],
    )
    assert HVACAgent().is_relevant(world, loud) is True


def test_state_refresh_without_reasons_is_still_ignored_by_every_agent(world):
    quiet = _event("environment.state_refresh")
    assert LightingAgent().is_relevant(world, quiet) is False
    assert HVACAgent().is_relevant(world, quiet) is False

    loud = _event("environment.state_refresh", significant_change_reasons=["room_occupancy"])
    assert LightingAgent().is_relevant(world, loud) is True
    assert HVACAgent().is_relevant(world, loud) is True


def test_light_level_threshold_goes_to_lighting_only(world):
    """§7「environment.light_level_threshold → lights, curtains | sensors」。"""

    event = _event(
        "environment.light_level_threshold",
        room_id="living_room",
        significant_change_reasons=["light_level_threshold"],
    )
    assert LightingAgent().is_relevant(world, event) is True
    assert HVACAgent().is_relevant(world, event) is False


def test_user_command_device_filter_is_unchanged(world):
    """S1 行为：指名设备的用户命令只落到控这台设备的 agent。"""

    to_light = _event("user.command", device_id="light_living_01")
    assert LightingAgent().is_relevant(world, to_light) is True
    assert HVACAgent().is_relevant(world, to_light) is False

    to_ac = _event("user.command", device_id="ac_living_01")
    assert HVACAgent().is_relevant(world, to_ac) is True
    assert LightingAgent().is_relevant(world, to_ac) is False

    # 未指名设备（自然语言指令）仍然广播给所有 agent
    freeform = _event("user.command", text="有点热")
    assert LightingAgent().is_relevant(world, freeform) is True
    assert HVACAgent().is_relevant(world, freeform) is True

    # 世界里查不到的设备 id 不做静默丢弃（S1 原语义）
    unknown = _event("user.command", device_id="device_ghost_99")
    assert LightingAgent().is_relevant(world, unknown) is True
    assert HVACAgent().is_relevant(world, unknown) is True


def test_rich_user_events_still_reach_both_domain_agents(world):
    """§6.1/§6.2 的到家/离家仍然是舒适面事件：灯与空调都要开 episode。"""

    for event_type in ("user.arrives_home", "user.leaves_home", "user.starts_activity"):
        event = _event(event_type, user_id="user_01", to_room="living_room")
        assert LightingAgent().is_relevant(world, event) is True, event_type
        assert HVACAgent().is_relevant(world, event) is True, event_type


def test_unsubscribed_event_type_is_still_rejected_first(world):
    assert LightingAgent().is_relevant(world, _event("system.timer_tick")) is False


# --------------------------------------------------------------- 4. 映射表本身


def test_every_root_event_type_declares_its_device_relevance():
    """§7「Every root event type must declare its default device relevance」。"""

    for event_type in ALL_ROOT_EVENT_TYPES:
        assert actionable_device_types(event_type), event_type


# --------------------------------------------------------------- 5. 运行期效果


@pytest.mark.anyio
async def test_offline_camera_produces_no_agent_episode_at_runtime():
    """端到端：摄像头掉线不再触发任何一条推理链（episode 爆炸的直接度量）。"""

    engine = SimulationEngine(
        event_bus=EventBus(),
        state_manager=_init_default_state(),
        connection_manager=ConnectionManager(),
        llm_provider=_DisabledProvider(),
    )
    collected: list[SimEvent] = []

    async def collect(event: SimEvent) -> None:
        collected.append(event)

    engine.event_bus.subscribe("*", collect)

    camera_offline = _event(
        "device.offline",
        device_id="camera_living_02",
        device_type="camera",
        online=False,
    )
    ac_offline = _event(
        "device.offline",
        device_id="ac_living_01",
        device_type="hvac",
        online=False,
    )
    await engine.event_bus.publish(camera_offline)
    await engine.event_bus.publish(ac_offline)
    assert await engine.agent_runtime.wait_for_idle(timeout=10.0)
    await engine.close()

    def reasoning_agents(root: SimEvent) -> set[str]:
        return {
            str(event.data.get("agent_id"))
            for event in collected
            if event.event_type.startswith("reasoning.")
            and event.correlation_id == root.correlation_id
        }

    assert reasoning_agents(camera_offline) == set()
    assert reasoning_agents(ac_offline) == {"hvac_agent"}
