"""SecurityAgent —— §8.2 安防域 agent（§9.1 security 档 + safety 档的第一个真实生产者）。

为什么这个文件存在（审计 §六 逐条对应）：

* §9.1 把 ``safety`` 排在最高档、``security`` 排在第三档，可是 S2 结束时**没有任何 agent**
  产出过这两档提案——仲裁器的最高两档只被合成夹具走过，"安全优先"在真实链路里是空话。
* §7 的 ``device.offline`` 行 default_policy 写的是 *fail closed and explain*，而 S2 之前
  设备掉线只会让相关 agent 少跑一轮，既不 fail 也不 explain。

三条实现自律：

1. **不对摄像头发写命令**。§3.2 能力矩阵里 camera 只有 ``view``/``online`` 两条**只读**
   能力，"布防"在设备层没有可写落点。本 agent 把布防表达成两件可断言的事：提案落在
   ``security`` 档 + 摄像头覆盖房间里的**门口灯**（§7 security.presence_detected 行原文
   的 "entry lights"，那行注释已经写明"真正的门口灯收口留给 agent 的 allowed_command_specs"）。
   把只读能力当写口发出去，只会在演示里变成一串 ``capability_not_writable`` 失败命令。
2. **相关面靠事件族收口，不靠 §7 设备面兜底**。安防 agent 控 light，如果沿用基类那条
   "控的设备类型与 §7 相关面有交集就开一轮"，它会为每一条环境刷新、每一次进出房间都
   跑一轮推理——episode 数按 agent 数线性膨胀。
3. **无事可做要说出来**（§8.4）：撤防、覆盖未丢失都走 ``no_action_needed`` + 明确理由，
   而不是返回零命令了事。
"""

from __future__ import annotations

from typing import Any

from backend.agents.base import BaseAgent, ProposalReview
from backend.agents.contracts import DomainTask, PriorityLevel, ProposalOutcome
from backend.agents.types import AgentCommandProposal, PriorityLabel
from backend.engine.event_bus import SimEvent
from backend.engine.event_types import (
    DEVICE_OFFLINE,
    DEVICE_RECOVERED,
    SAFETY_SMOKE_DETECTED,
    SECURITY_DOOR_OPENED,
    SECURITY_PRESENCE_DETECTED,
    USER_ARRIVES_HOME,
    USER_LEAVES_HOME,
)
from backend.engine.state import DeviceState, WorldState

__all__ = [
    "ARMED_BRIGHTNESS",
    "ALERT_BRIGHTNESS",
    "EVACUATION_BRIGHTNESS",
    "SECURITY_AGENT_ID",
    "SECURITY_ROOT_EVENT_TYPES",
    "CoverageReport",
    "SecurityAgent",
]


SECURITY_AGENT_ID = "security_agent"

# 本 agent 只对这七类根事件开 episode（自律 2）。顺序排序是为了订阅注册序确定。
SECURITY_ROOT_EVENT_TYPES: frozenset[str] = frozenset(
    {
        SAFETY_SMOKE_DETECTED,
        SECURITY_PRESENCE_DETECTED,
        SECURITY_DOOR_OPENED,
        USER_ARRIVES_HOME,
        USER_LEAVES_HOME,
        DEVICE_OFFLINE,
        DEVICE_RECOVERED,
    }
)

# 布防档：留一档"有人在家"的假象照明（威慑），不是照明舒适档。
ARMED_BRIGHTNESS = 25
# 告警档 / 疏散档：全开。两者数值相同但语义不同，分开命名以免有人"顺手合并"。
ALERT_BRIGHTNESS = 100
EVACUATION_BRIGHTNESS = 100


class CoverageReport:
    """一次安防覆盖观测：哪些房间有在线摄像头、哪些摄像头掉线了。

    刻意是普通对象而不是 pydantic 模型：它不跨进程、不进事件流，只是 agent 内部的一次
    读数快照。房间列表一律升序——canonical trace 不能有集合迭代序泄漏。
    """

    __slots__ = ("covered_rooms", "offline_cameras", "cameras_by_room")

    def __init__(
        self,
        *,
        covered_rooms: tuple[str, ...],
        offline_cameras: tuple[str, ...],
        cameras_by_room: dict[str, tuple[str, ...]],
    ) -> None:
        self.covered_rooms = covered_rooms
        self.offline_cameras = offline_cameras
        self.cameras_by_room = cameras_by_room

    def online_cameras_in(self, room_id: str) -> tuple[str, ...]:
        offline = set(self.offline_cameras)
        return tuple(
            camera for camera in self.cameras_by_room.get(room_id, ()) if camera not in offline
        )


def _camera_is_online(device: DeviceState) -> bool:
    """摄像头是否在线。

    §3.2 把 ``online`` 声明为 camera 的只读能力，注册表同时把它镜像进
    ``state.extra["online"]``（前端 CameraPanel 读的正是它）；缺省视为在线，与
    :func:`backend.engine.observation.device_reports_online` 同口径。
    """

    return bool(device.state.power) and bool(device.state.extra.get("online", True))


class SecurityAgent(BaseAgent):
    """安防姿态：布防 / 撤防 / 告警 / 覆盖丢失兜底。"""

    agent_role = "security"
    subscribed_event_types = tuple(sorted(SECURITY_ROOT_EVENT_TYPES))

    def __init__(self) -> None:
        super().__init__(agent_id=SECURITY_AGENT_ID, name="Security Agent")

    # ------------------------------------------------------------- 基本声明

    def get_controlled_device_types(self) -> list[str]:
        # camera 只读（观测覆盖用），light 才是唯一可写落点（§7「entry lights」）。
        return ["camera", "light"]

    def determine_priority(
        self,
        world_state: WorldState,
        root_event: SimEvent,
    ) -> PriorityLabel:
        """迁移期的旧六值标签（AgentDecisionEnvelope 与旧 Arbiter 仍读它）。

        旧表**没有 security 这一档**，只能二选一，两边都是错的：

        * 借 ``safety`` 表达 → 旧 ``PRIORITY_RANK`` 里 safety 与 direct_user_command **同分**，
          于是"离家布防"能和用户亲手下的命令平起平坐，用户覆盖变成掷硬币；
        * 借 ``user_comfort`` 表达 → 与照明域同分，安防略被低估。

        选后者：**宁可低估安防，也不能让它压过用户明示指令**。真正的 §9.1 档位在
        :meth:`proposal_priority`，S3-T5 换上全序仲裁器后这里的取舍即失效。
        烟雾是例外——它在新旧两张表里都该是最高档。
        """

        if root_event.event_type == SAFETY_SMOKE_DETECTED:
            return "safety"
        return "user_comfort"

    def proposal_priority(self, world_state: WorldState, root_event: SimEvent) -> PriorityLevel:
        """§9.1：烟雾走 safety（最高档），其余安防姿态走 security。"""

        if root_event.event_type == SAFETY_SMOKE_DETECTED:
            return PriorityLevel.SAFETY
        return PriorityLevel.SECURITY

    def proposal_intent(self, world_state: WorldState, root_event: SimEvent) -> str:
        return {
            SAFETY_SMOKE_DETECTED: "烟雾报警：疏散照明 fail-closed",
            SECURITY_PRESENCE_DETECTED: "无人在家时检测到活动：升高告警照明",
            SECURITY_DOOR_OPENED: "无人在家时门被打开：升高告警照明",
            USER_LEAVES_HOME: "离家布防：摄像头覆盖区留威慑照明",
            USER_ARRIVES_HOME: "到家撤防",
            DEVICE_OFFLINE: "安防覆盖变更：核对摄像头在线情况",
            DEVICE_RECOVERED: "安防覆盖恢复：核对摄像头在线情况",
        }.get(root_event.event_type, f"security response to {root_event.event_type}")

    # ------------------------------------------------------------- 相关面

    def is_relevant(self, world_state: WorldState, root_event: SimEvent) -> bool:
        if root_event.event_type not in SECURITY_ROOT_EVENT_TYPES:
            return False
        # 设备可用性事件是**设备族**事件：只有摄像头的在线状态改变才影响安防覆盖。
        if root_event.event_type in (DEVICE_OFFLINE, DEVICE_RECOVERED):
            return self._affected_device_type(world_state, root_event) == "camera"
        return True

    def get_relevant_devices(
        self, world_state: WorldState, root_event: SimEvent
    ) -> list[DeviceState]:
        """相关设备 = 焦点房间里的摄像头（观测）+ 灯（唯一可写落点）。"""

        focus = self._focus_rooms(world_state, root_event)
        devices = [
            device
            for device in world_state.devices.values()
            if device.type in {"camera", "light"}
        ]
        if focus:
            devices = [device for device in devices if device.location.room in focus]
        return sorted(devices, key=lambda device: device.id)

    def get_allowed_command_specs(
        self,
        world_state: WorldState,
        root_event: SimEvent,
    ) -> list[dict[str, Any]]:
        """白名单只含灯的 power/brightness——摄像头一条写口都不给（自律 1）。"""

        specs: list[dict[str, Any]] = []
        for device in self.get_relevant_devices(world_state, root_event):
            if device.type != "light":
                continue
            specs.append({"device_id": device.id, "property": "power"})
            specs.append({"device_id": device.id, "property": "extra.brightness"})
        return specs

    # ------------------------------------------------------------- 规则决策

    def decide(self, world_state: WorldState) -> list[dict]:
        """无事件上下文时的姿态：全屋无人则按布防档处理，否则不动。

        安防动作是**事件驱动**的（见 :meth:`decide_for_event`）；这条与事件无关的入口
        只在 ``AgentRuntime.step`` 那种轮询调用里出现，保守到底。
        """

        if self._home_occupied(world_state):
            return []
        return self._armed_posture_actions(world_state, self.coverage_report(world_state))

    def decide_for_event(self, world_state: WorldState, root_event: SimEvent) -> list[dict]:
        coverage = self.coverage_report(world_state)
        event_type = root_event.event_type

        if event_type == SAFETY_SMOKE_DETECTED:
            return self._evacuation_actions(world_state, root_event)

        if event_type in (SECURITY_PRESENCE_DETECTED, SECURITY_DOOR_OPENED):
            # 有人在家时的活动信号不是入侵；照明交还舒适域。
            if self._home_occupied(world_state):
                return []
            return self._alert_actions(world_state, root_event)

        if event_type == USER_LEAVES_HOME:
            return self._armed_posture_actions(world_state, coverage)

        if event_type == USER_ARRIVES_HOME:
            # 撤防没有设备动作：威慑照明交还 lighting agent（由它按钟点重算）。
            return []

        if event_type in (DEVICE_OFFLINE, DEVICE_RECOVERED):
            # 覆盖丢失时**本来**要做的补偿动作；到底发不发由 review_proposal 决定
            # （§7 fail closed and explain）。
            return self._alert_actions(world_state, root_event)

        return []

    # ------------------------------------------------- §8.4 非动作表达（fail closed）

    def review_proposal(
        self,
        *,
        world_state: WorldState,
        root_event: SimEvent,
        commands: list[AgentCommandProposal],
        domain_task: DomainTask | None = None,
    ) -> ProposalReview | None:
        if root_event.event_type == USER_ARRIVES_HOME:
            return ProposalReview(
                outcome=ProposalOutcome.NO_ACTION_NEEDED,
                noop_reason="有人到家，安防撤防；威慑照明交还照明域按钟点重算",
            )

        if root_event.event_type != DEVICE_OFFLINE:
            return None

        device_id = str(root_event.data.get("device_id") or "")
        coverage = self.coverage_report(world_state)
        room_id = self._device_room(world_state, device_id)
        survivors = tuple(
            camera for camera in coverage.online_cameras_in(room_id) if camera != device_id
        )
        if survivors:
            return ProposalReview(
                outcome=ProposalOutcome.NO_ACTION_NEEDED,
                noop_reason=(
                    f"{device_id} 掉线，但 {room_id} 仍由 {', '.join(survivors)} 覆盖，"
                    f"安防姿态无需变更"
                ),
            )

        # §7「fail closed and explain」：房间最后一台摄像头掉线 → 覆盖不可验证。
        # 此时任何"按覆盖仍在"的姿态动作都是猜的，宁可拒绝并把想做的事留痕，
        # 也不要让研究者以为安防还在正常工作。
        return ProposalReview(
            outcome=ProposalOutcome.UNSAFE_REJECTED,
            noop_reason=(
                f"{room_id} 已无在线摄像头，安防覆盖不可验证；按 §7 fail-closed 拒绝执行"
                f"依赖覆盖假设的姿态动作，等待人工介入或设备恢复"
            ),
            risks=[
                f"{device_id} 离线：{room_id} 安防覆盖不可验证",
                "本轮扣下的补偿照明未执行，房间处于无监控状态",
            ],
        )

    # ------------------------------------------------------------- 观测助手

    def coverage_report(self, world_state: WorldState) -> CoverageReport:
        cameras_by_room: dict[str, list[str]] = {}
        offline: list[str] = []
        for device in sorted(world_state.devices.values(), key=lambda item: item.id):
            if device.type != "camera":
                continue
            cameras_by_room.setdefault(device.location.room, []).append(device.id)
            if not _camera_is_online(device):
                offline.append(device.id)

        covered = tuple(
            sorted(
                room_id
                for room_id, cameras in cameras_by_room.items()
                if any(camera not in set(offline) for camera in cameras)
            )
        )
        return CoverageReport(
            covered_rooms=covered,
            offline_cameras=tuple(offline),
            cameras_by_room={
                room_id: tuple(cameras) for room_id, cameras in cameras_by_room.items()
            },
        )

    @staticmethod
    def _home_occupied(world_state: WorldState) -> bool:
        return any(room.occupancy for room in world_state.rooms.values())

    @staticmethod
    def _device_room(world_state: WorldState, device_id: str) -> str:
        device = world_state.devices.get(device_id)
        return device.location.room if device is not None else ""

    def _focus_rooms(self, world_state: WorldState, root_event: SimEvent) -> set[str]:
        """事件点名的房间；点不出来就不收窄（返回空集）。"""

        rooms: set[str] = set()
        for key in ("room_id", "to_room", "from_room"):
            room_id = str(root_event.data.get(key) or "")
            if room_id and room_id in world_state.rooms:
                rooms.add(room_id)
        device_id = str(root_event.data.get("device_id") or "")
        if device_id:
            room_id = self._device_room(world_state, device_id)
            if room_id:
                rooms.add(room_id)
        return rooms

    def _lights_in(self, world_state: WorldState, room_ids: set[str] | None) -> list[DeviceState]:
        lights = [device for device in world_state.devices.values() if device.type == "light"]
        if room_ids is not None:
            lights = [light for light in lights if light.location.room in room_ids]
        return sorted(lights, key=lambda device: device.id)

    # ------------------------------------------------------------- 姿态动作

    def _armed_posture_actions(
        self, world_state: WorldState, coverage: CoverageReport
    ) -> list[dict]:
        """布防：只在**有在线摄像头覆盖**的房间留威慑照明（§7「entry lights」）。"""

        actions: list[dict] = []
        for room_id in coverage.covered_rooms:
            cameras = coverage.online_cameras_in(room_id)
            witness = cameras[0] if cameras else ""
            for light in self._lights_in(world_state, {room_id}):
                reason = f"离家布防：{room_id} 由 {witness} 覆盖，保留威慑照明"
                if not light.state.power:
                    actions.append(
                        {
                            "device_id": light.id,
                            "property": "power",
                            "value": True,
                            "reason": reason,
                        }
                    )
                if int(light.state.extra.get("brightness", 0)) != ARMED_BRIGHTNESS:
                    actions.append(
                        {
                            "device_id": light.id,
                            "property": "extra.brightness",
                            "value": ARMED_BRIGHTNESS,
                            "reason": reason,
                        }
                    )
        return actions

    def _alert_actions(self, world_state: WorldState, root_event: SimEvent) -> list[dict]:
        """告警 / 覆盖丢失补偿：焦点房间的灯全开。"""

        focus = self._focus_rooms(world_state, root_event) or None
        reason = f"安防告警（{root_event.event_type}）：升高照明以提高可见度与威慑"
        actions: list[dict] = []
        for light in self._lights_in(world_state, focus):
            actions.append(
                {"device_id": light.id, "property": "power", "value": True, "reason": reason}
            )
            actions.append(
                {
                    "device_id": light.id,
                    "property": "extra.brightness",
                    "value": ALERT_BRIGHTNESS,
                    "reason": reason,
                }
            )
        return actions

    def _evacuation_actions(self, world_state: WorldState, root_event: SimEvent) -> list[dict]:
        """烟雾：疏散照明。fail-closed —— 不看钟点、不看能耗、不看有没有人。"""

        focus = self._focus_rooms(world_state, root_event) or None
        reason = "烟雾报警：疏散照明全开（fail-closed，不受钟点/能耗策略影响）"
        actions: list[dict] = []
        for light in self._lights_in(world_state, focus):
            actions.append(
                {"device_id": light.id, "property": "power", "value": True, "reason": reason}
            )
            actions.append(
                {
                    "device_id": light.id,
                    "property": "extra.brightness",
                    "value": EVACUATION_BRIGHTNESS,
                    "reason": reason,
                }
            )
        return actions
