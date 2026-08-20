"""SceneAgent —— 命名场景下推到后端：数据定义 + **显式**设备提案。

被推翻的现状（S3-T4 的动机）：``frontend/src/components/dashboard/SceneSelector.vue``
在浏览器里循环发 2×N 条 ``CMD_DEVICE_CONTROL``。后果有三条，每条都打在产品定位上——

1. 后端**看不见**"这是一次场景切换"：可观测性面板只看到 N 条互不相干的直控命令，
   拼不出因果链，而"看得见 Agent 的推理链路"正是这个平台的卖点；
2. 场景语义只存在于 .vue 的 switch 里，headless 场景脚本与 S4 评估器无从复用；
3. 直控命令绕过编排与仲裁，§9 的优先级全序对"场景"完全不生效。

下推后：场景定义是 :file:`backend/config/scene_definitions.yaml`（数据），SceneAgent 把它
展开成**显式** :class:`~backend.agents.types.AgentCommandProposal`，与其他 agent 一样
进仲裁、进 CommandExecutor。**本模块不写世界**（tests/test_scene_agent.py 有一条源码级
钉子在盯）——隐藏写入正是 spec 明令禁止的形态。

前端配套（S3 review minor「前门从未打开」的收口，已落地）：``SceneSelector.vue`` 的 switch
换成了一条 ``sendCommand('CMD_SCENE_APPLY', { scene_id })``，WS 入口是
``backend/main.py::_handle_scene_apply``（结构守卫 :class:`SceneApplyPayload` → 未知场景当场
回 ERROR → 一条带 ``scene_id`` 的 ``user.command`` 根事件），协议见
``docs/architecture/ws-protocol.md``。本 agent 仍然只认根事件，不认 WS 消息——消息与事件的
翻译只发生在 main.py 那一层。
"""

from __future__ import annotations

from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from backend.agents.base import BaseAgent, ProposalReview
from backend.agents.contracts import DomainTask, PriorityLevel, ProposalOutcome
from backend.agents.types import AgentCommandProposal, PriorityLabel
from backend.config.device_registry import build_default_rooms
from backend.execution.capability_matrix import (
    CAPABILITY_MATRIX,
    get_writable_capability_names,
)
from backend.engine.event_bus import SimEvent
from backend.engine.event_types import USER_COMMAND
from backend.engine.state import DeviceState, WorldState

__all__ = [
    "SCENE_AGENT_ID",
    "SCENE_APPLY_MESSAGE_TYPE",
    "SCENE_DEFINITIONS_PATH",
    "SceneAgent",
    "SceneApplyPayload",
    "SceneDefinition",
    "SceneDefinitionError",
    "SceneDefinitionErrorCode",
    "SceneLibrary",
    "SceneSelector",
    "SceneStep",
    "load_scene_definitions",
    "get_scene_definitions",
    "clear_scene_cache",
]


SCENE_AGENT_ID = "scene_agent"
#: 新增的 WS 消息类型：一条消息 = 一次场景切换（取代前端的 N 条直控）。
SCENE_APPLY_MESSAGE_TYPE = "CMD_SCENE_APPLY"
SCENE_DEFINITIONS_PATH: Path = Path(__file__).resolve().parents[1] / "config" / "scene_definitions.yaml"
SUPPORTED_SCHEMA_VERSIONS: frozenset[int] = frozenset({1})


class SceneApplyPayload(BaseModel):
    """``CMD_SCENE_APPLY`` 入站载荷结构守卫。

    与消息类型常量放在同一个模块，是为了让"这条消息长什么样"只有一处真相：main.py 只做
    翻译（消息 → 根事件），场景语义（有哪些场景、场景是什么）全部留在这里与
    :file:`backend/config/scene_definitions.yaml`。

    只做结构校验——``scene_id`` 存不存在是**语义**问题，由 :func:`get_scene_definitions`
    在入口处回答（未知场景当场回 ERROR，而不是开一条只会 no-op 的 episode）。
    """

    scene_id: str = Field(min_length=1)


class SceneDefinitionErrorCode(str, Enum):
    """加载期失败原因（与 :class:`~backend.config.event_mapping.EventMappingErrorCode` 同风格）。"""

    FILE_NOT_FOUND = "file_not_found"
    INVALID_DOCUMENT = "invalid_document"
    UNSUPPORTED_SCHEMA_VERSION = "unsupported_schema_version"
    UNKNOWN_DEVICE_TYPE = "unknown_device_type"
    UNKNOWN_CAPABILITY = "unknown_capability"
    CAPABILITY_NOT_WRITABLE = "capability_not_writable"
    UNKNOWN_ROOM = "unknown_room"


class SceneDefinitionError(Exception):
    """场景定义加载失败。**加载期抛**，不留到运行期变成静默漏配。"""

    def __init__(self, code: SceneDefinitionErrorCode, message: str) -> None:
        super().__init__(f"[{code.value}] {message}")
        self.code = code
        self.message = message


class SceneSelector(BaseModel):
    """一步作用在哪些设备上。

    三个维度取**交集**（都为空 = 匹配全部，等价于 .vue 里 ``dev.type === 'light'`` 那种
    "全屋同类型"写法）。``device_ids`` 保留给"点名某几台"的场景，当前表里没用到，
    但 schema 先留位——否则以后要加就得改所有已存在的 YAML。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    device_type: str | None = None
    device_ids: tuple[str, ...] = ()
    room_ids: tuple[str, ...] = ()

    def matches(self, device: DeviceState) -> bool:
        if self.device_type is not None and device.type != self.device_type:
            return False
        if self.device_ids and device.id not in self.device_ids:
            return False
        if self.room_ids and device.location.room not in self.room_ids:
            return False
        return True


class SceneStep(BaseModel):
    """场景里的一行：给一组设备的某个属性设一个值。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    selector: SceneSelector
    property: str
    value: Any

    @property
    def capability(self) -> str:
        """去掉 ``extra.`` 前缀后的能力名（与 executor 的归一口径一致）。"""

        return self.property.removeprefix("extra.")


class SceneDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scene_id: str
    label: str = ""
    description: str = ""
    source: str = ""
    steps: tuple[SceneStep, ...] = ()

    @property
    def device_types(self) -> tuple[str, ...]:
        """本场景涉及的设备类型（升序）。"""

        return tuple(
            sorted({step.selector.device_type for step in self.steps if step.selector.device_type})
        )


class SceneLibrary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int
    scenes: dict[str, SceneDefinition]
    path: Path | None = None

    @property
    def device_types(self) -> tuple[str, ...]:
        types: set[str] = set()
        for scene in self.scenes.values():
            types.update(scene.device_types)
        return tuple(sorted(types))

    def get(self, scene_id: str) -> SceneDefinition | None:
        return self.scenes.get(scene_id)


# --------------------------------------------------------------------------- #
# 加载与校验
# --------------------------------------------------------------------------- #


def _validate_step(scene_id: str, index: int, step: SceneStep) -> None:
    where = f"scene '{scene_id}' step[{index}]"
    device_type = step.selector.device_type
    if device_type is None:
        # 没有类型限定时无法做能力校验：要求点名 device_ids，否则这一步就是"对全屋乱写"。
        if not step.selector.device_ids:
            raise SceneDefinitionError(
                SceneDefinitionErrorCode.INVALID_DOCUMENT,
                f"{where}: selector 必须至少给出 device_type 或 device_ids",
            )
        return

    if device_type not in CAPABILITY_MATRIX:
        raise SceneDefinitionError(
            SceneDefinitionErrorCode.UNKNOWN_DEVICE_TYPE,
            f"{where}: 未知设备类型 '{device_type}'（§3.2 能力矩阵里没有）",
        )

    writable = get_writable_capability_names(device_type)  # type: ignore[arg-type]
    known = {spec.name for spec in CAPABILITY_MATRIX[device_type]}  # type: ignore[index]
    capability = step.capability
    if capability not in known:
        raise SceneDefinitionError(
            SceneDefinitionErrorCode.UNKNOWN_CAPABILITY,
            f"{where}: '{device_type}' 没有能力 '{capability}'",
        )
    if capability not in writable:
        raise SceneDefinitionError(
            SceneDefinitionErrorCode.CAPABILITY_NOT_WRITABLE,
            f"{where}: '{device_type}.{capability}' 是只读能力，写进场景只会得到失败命令",
        )

    known_rooms = set(build_default_rooms())
    for room_id in step.selector.room_ids:
        if room_id not in known_rooms:
            raise SceneDefinitionError(
                SceneDefinitionErrorCode.UNKNOWN_ROOM,
                f"{where}: 未知房间 '{room_id}'",
            )


def load_scene_definitions(path: Path | str | None = None) -> SceneLibrary:
    """读一份场景定义并做加载期硬校验（失败即抛，不留运行期静默漏配）。"""

    target = Path(path) if path is not None else SCENE_DEFINITIONS_PATH
    if not target.exists():
        raise SceneDefinitionError(
            SceneDefinitionErrorCode.FILE_NOT_FOUND, f"场景定义文件不存在：{target}"
        )

    raw = yaml.safe_load(target.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise SceneDefinitionError(
            SceneDefinitionErrorCode.INVALID_DOCUMENT, f"{target}: 顶层必须是映射"
        )

    schema_version = raw.get("schema_version")
    if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        raise SceneDefinitionError(
            SceneDefinitionErrorCode.UNSUPPORTED_SCHEMA_VERSION,
            f"{target}: 不支持的 schema_version={schema_version!r}",
        )

    raw_scenes = raw.get("scenes")
    if not isinstance(raw_scenes, dict):
        raise SceneDefinitionError(
            SceneDefinitionErrorCode.INVALID_DOCUMENT, f"{target}: scenes 必须是映射"
        )

    scenes: dict[str, SceneDefinition] = {}
    for scene_id, body in raw_scenes.items():
        if not isinstance(body, dict):
            raise SceneDefinitionError(
                SceneDefinitionErrorCode.INVALID_DOCUMENT,
                f"scene '{scene_id}': 必须是映射",
            )
        try:
            definition = SceneDefinition(scene_id=str(scene_id), **body)
        except Exception as exc:  # pydantic 校验失败也归到加载期
            raise SceneDefinitionError(
                SceneDefinitionErrorCode.INVALID_DOCUMENT,
                f"scene '{scene_id}': {exc}",
            ) from exc
        for index, step in enumerate(definition.steps):
            _validate_step(str(scene_id), index, step)
        scenes[str(scene_id)] = definition

    return SceneLibrary(schema_version=int(schema_version), scenes=scenes, path=target)


@lru_cache(maxsize=1)
def get_scene_definitions() -> SceneLibrary:
    """默认场景表（进程级缓存）。"""

    return load_scene_definitions()


def clear_scene_cache() -> None:
    get_scene_definitions.cache_clear()


# --------------------------------------------------------------------------- #
# Agent
# --------------------------------------------------------------------------- #


def scene_id_of(root_event: SimEvent) -> str | None:
    """从根事件里取场景 id；取不到返回 None（由 agent 报 missing_observations）。"""

    raw = root_event.data.get("scene_id")
    return str(raw) if raw else None


class SceneAgent(BaseAgent):
    """把命名场景展开成显式设备提案（§9.1 ambience 档）。"""

    agent_role = "scene"

    def __init__(self, library: SceneLibrary | None = None) -> None:
        super().__init__(agent_id=SCENE_AGENT_ID, name="Scene Agent")
        self._library = library if library is not None else get_scene_definitions()

    @property
    def library(self) -> SceneLibrary:
        return self._library

    # ------------------------------------------------------------- 基本声明

    def get_controlled_device_types(self) -> list[str]:
        """受控类型从**场景表**推导，不写死——加一个场景就自动扩面（§7「data, not branching」）。"""

        return list(self._library.device_types)

    def determine_priority(
        self,
        world_state: WorldState,
        root_event: SimEvent,
    ) -> PriorityLabel:
        """迁移期旧标签。

        刻意**不**用 ``direct_user_command``：旧表里它与 safety 同分，一次场景切换就会
        压过安防与安全动作。``convenience`` 经 ``LEGACY_PRIORITY_MIGRATION`` 恰好映射到
        §9.1 的 ``ambience``，新旧两张表因此保持一致（见 :meth:`proposal_priority`）。
        """

        return "convenience"

    def proposal_priority(self, world_state: WorldState, root_event: SimEvent) -> PriorityLevel:
        """§9.1 ambience 档。

        刻意**不**是 explicit_user：explicit_user 是"用户点名某台设备做某件事"，
        而场景是一整套氛围预设，理应让位于安全/安防/舒适。真正的单设备直控仍走
        CMD_DEVICE_CONTROL 那条腿（S3-T5 会把它送进仲裁并落在 explicit_user 档）。
        """

        return PriorityLevel.AMBIENCE

    def proposal_intent(self, world_state: WorldState, root_event: SimEvent) -> str:
        scene_id = scene_id_of(root_event)
        definition = self._library.get(scene_id) if scene_id else None
        if definition is not None:
            return f"应用场景「{definition.label or definition.scene_id}」"
        return "场景切换请求"

    # ------------------------------------------------------------- 相关面

    def is_relevant(self, world_state: WorldState, root_event: SimEvent) -> bool:
        """只对**场景切换**根事件开一轮：带 scene_id 的 user.command，或 CMD_SCENE_APPLY。

        普通的 CMD_DEVICE_CONTROL 直控不在此列（那是单设备命令，不是场景）。
        """

        if root_event.event_type != USER_COMMAND:
            return False
        if root_event.data.get("message_type") == SCENE_APPLY_MESSAGE_TYPE:
            return True
        return scene_id_of(root_event) is not None

    def get_relevant_devices(
        self, world_state: WorldState, root_event: SimEvent
    ) -> list[DeviceState]:
        scene_id = scene_id_of(root_event)
        definition = self._library.get(scene_id) if scene_id else None
        if definition is None:
            return []
        matched = {
            device.id: device
            for device in world_state.devices.values()
            for step in definition.steps
            if step.selector.matches(device)
        }
        return [matched[device_id] for device_id in sorted(matched)]

    def get_allowed_command_specs(
        self,
        world_state: WorldState,
        root_event: SimEvent,
    ) -> list[dict[str, Any]]:
        return [
            {"device_id": action["device_id"], "property": action["property"]}
            for action in self.decide_for_event(world_state, root_event)
        ]

    # ------------------------------------------------------------- 场景展开

    def decide(self, world_state: WorldState) -> list[dict]:
        """场景必须由事件点名，没有"默认场景"——返回空是正确行为。"""

        return []

    def decide_for_event(self, world_state: WorldState, root_event: SimEvent) -> list[dict]:
        scene_id = scene_id_of(root_event)
        if scene_id is None:
            return []
        definition = self._library.get(scene_id)
        if definition is None:
            return []
        return self.expand(definition, world_state)

    def expand(self, definition: SceneDefinition, world_state: WorldState) -> list[dict]:
        """场景 → 显式命令列表。

        顺序 = YAML 步骤序 × 设备 id 升序（确定性门要求它稳定，且与"先开机再调亮度"
        这种步骤内在依赖一致——所以**不**按设备聚合重排）。
        """

        actions: list[dict] = []
        devices = sorted(world_state.devices.values(), key=lambda device: device.id)
        for step in definition.steps:
            for device in devices:
                if not step.selector.matches(device):
                    continue
                actions.append(
                    {
                        "device_id": device.id,
                        "property": step.property,
                        "value": step.value,
                        "reason": (
                            f"场景「{definition.label or definition.scene_id}」"
                            f"（{definition.scene_id}）：{step.property} → {step.value!r}"
                        ),
                    }
                )
        return actions

    # ------------------------------------------------- §8.4 非动作表达

    def review_proposal(
        self,
        *,
        world_state: WorldState,
        root_event: SimEvent,
        commands: list[AgentCommandProposal],
        domain_task: DomainTask | None = None,
    ) -> ProposalReview | None:
        scene_id = scene_id_of(root_event)
        if scene_id is None:
            # §8.4 第三种表达：观测不足要**点名缺了什么**。
            return ProposalReview(
                outcome=ProposalOutcome.MISSING_OBSERVATIONS,
                noop_reason=(
                    f"{SCENE_APPLY_MESSAGE_TYPE} 未携带 scene_id，无法确定要应用哪个场景"
                ),
                missing_observations=["scene_id"],
            )
        if self._library.get(scene_id) is None:
            return ProposalReview(
                outcome=ProposalOutcome.NO_ACTION_NEEDED,
                noop_reason=(
                    f"未知场景 id '{scene_id}'；已知场景："
                    f"{', '.join(sorted(self._library.scenes))}"
                ),
            )
        return None
