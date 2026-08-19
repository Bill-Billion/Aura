"""单一来源 seed 化随机数（spec §4.5 事件生成模式 + §11 run 可复现模型）。

在 S2 之前，本仿真"确定性"只是巧合——产品代码一个随机数都没抽过，因此也从来没有
seed 契约。S2 要引入 stochastic 事件、观测噪声、用户行为抖动，必须先把随机性的
规矩立好，否则可复现性会在第一个随机消费者接进来的当天就失效。

本模块给出唯一的随机源契约：

- 一个 run 只有 **一个 seed**（§11 run 元数据必填字段）；
- 每个随机消费者向 :class:`SimRandom` 申请一条 **命名子流**（``user_sim`` /
  ``env_noise`` / ``stochastic_events`` / ``observation_noise`` …），
  子流 seed = 纯函数 ``derive_stream_seed(seed, name)``；
- 因此 **新增一个消费者不会移动任何既有流的游标**。这是本模块存在的核心理由：
  若按"申请顺序"依次 spawn 子流（``random.Random().random()`` 逐个派生），S3 随手
  加一条新流就会把 S2 场景的回放结果整体改写，而测试大概率还是绿的——可复现性
  就此名存实亡；
- 被真正抽取过的流名会被记录，S2-T6 用 :meth:`SimStream.event_metadata` 给
  stochastic 事件打上 ``rng_stream`` 标（§4.5 第五项元数据），S2-T9 用
  :meth:`SimRandom.metadata` 把 seed 写进 run 工件。

刻意的边界：
- 只用标准库（``random.Random`` 的 Mersenne Twister 跨 CPython 版本稳定，
  ``hashlib.blake2b`` 跨进程稳定）——**不要**用内建 ``hash()``，它带进程盐，
  跨进程不一致，会让"同 seed 同结果"在换个进程后静默失效；
- :class:`SimStream` 直接继承 ``random.Random``，可以原样塞进既有
  ``rng: random.Random | None`` 接缝（见 backend/simulators/effects.py）；
- 本模块不解决"同 tick 事件顺序不可复现"（墙钟 timer + 并发 gather），那是 S2-T9
  的事——这里只保证 RNG 自身不成为额外的不确定性来源：子流互不影响、抽取顺序
  不跨流传染。
"""

from __future__ import annotations

import hashlib
import os
import random
import re
from enum import Enum
from typing import Any, Iterator, Mapping

# RNG 内部仍能消费 64 位无符号 seed；公开的 Scenario/API 契约另收窄到
# ``MAX_JSON_SAFE_SEED``，因为 JavaScript JSON number 超过 2**53-1 会静默丢精度。
MAX_SEED = 2**64 - 1
MAX_JSON_SAFE_SEED = 2**53 - 1

# 流名进事件元数据、run 工件文件名与前端筛选器，故限制为可安全序列化的窄字符集。
# 允许 ':' 是给未来的分片流留口（如 "stochastic_events:light_1"）。
_STREAM_NAME_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")

# blake2b 的 person 域把本派生函数与其它用途的散列隔开：即使别处也对同样的
# (seed, name) 做 blake2b，也不会撞出同一条流。改动它 = 改动所有历史 run 的回放结果。
_DERIVE_PERSON = b"aura-simrng-v1"

# 元数据格式版本。派生算法一旦变更必须 +1，否则旧 run.json 会被静默错误回放。
METADATA_VERSION = 1


class RngStream(str, Enum):
    """S2 已知的随机消费者（流名单一来源）。

    传字符串也合法（未来消费者不必回头改本枚举），但**已知**消费者必须走这里，
    避免 "env_noise" / "environment_noise" 这类同义流名把一条流劈成两条。
    """

    USER_SIM = "user_sim"
    ENV_NOISE = "env_noise"
    STOCHASTIC_EVENTS = "stochastic_events"
    OBSERVATION_NOISE = "observation_noise"


def normalize_stream_name(name: Any) -> str:
    """校验并归一流名；非法名早失败，绝不静默造一条新流。"""

    if isinstance(name, RngStream):
        return name.value
    if not isinstance(name, str):
        raise TypeError(f"stream name must be str or RngStream, got {type(name).__name__}")
    if not _STREAM_NAME_RE.match(name):
        raise ValueError(
            f"invalid rng stream name {name!r}: expected 1-64 chars of [A-Za-z0-9_.:-]"
        )
    return name


def validate_seed(seed: Any) -> int:
    """seed 必须是 [0, MAX_SEED] 的真整数。

    显式拒绝 ``bool``（``True`` 是 int 的子类，但 "seed: true" 只可能是 YAML 笔误）
    与 float/str（1.5、"1001" 落进 Mersenne Twister 会得到与整数完全不同的流）。
    """

    if isinstance(seed, bool) or not isinstance(seed, int):
        raise TypeError(f"seed must be an int, got {type(seed).__name__}")
    if not 0 <= seed <= MAX_SEED:
        raise ValueError(f"seed must be in [0, {MAX_SEED}], got {seed}")
    return seed


def derive_stream_seed(seed: int, name: Any) -> int:
    """(run seed, 流名) → 子流 seed，纯函数、跨进程稳定。

    按名字散列而非按申请顺序派生，是"新增消费者不扰动既有流"的全部实现。
    """

    seed = validate_seed(seed)
    stream_name = normalize_stream_name(name)
    digest = hashlib.blake2b(
        f"{seed}:{stream_name}".encode("utf-8"),
        digest_size=8,
        person=_DERIVE_PERSON,
    ).digest()
    return int.from_bytes(digest, "big")


def new_seed() -> int:
    """给没写 seed 的 run 生成一个可记录的 seed（§11：每个 run 都必须有 seed）。"""

    # 所有自动生成的 run metadata 都可能进入浏览器；只生成 JSON 可精确往返的整数。
    return int.from_bytes(os.urandom(8), "big") & MAX_JSON_SAFE_SEED


class SimStream(random.Random):
    """一条命名子流：``random.Random`` 的直接替代品 + 抽取计数。

    继承而非包装，是为了让既有 ``rng: random.Random | None`` 形参（见
    backend/simulators/effects.py）零改动接入。``random`` 与 ``getrandbits`` 是
    ``random.Random`` 所有高层方法（gauss/choice/randint/shuffle…）的两个底层出口，
    只在这两处计数即可覆盖全部抽取；两者同时覆写保持基类的
    ``_randbelow_with_getrandbits`` 选择不变，序列与基类完全一致。
    """

    def __init__(self, name: Any, run_seed: int) -> None:
        self._name = normalize_stream_name(name)
        self._run_seed = validate_seed(run_seed)
        self._stream_seed = derive_stream_seed(self._run_seed, self._name)
        self._draw_count = 0
        super().__init__(self._stream_seed)

    # —— 只读身份 ——

    @property
    def name(self) -> str:
        """流名，即事件元数据里的 ``rng_stream``。"""

        return self._name

    @property
    def run_seed(self) -> int:
        return self._run_seed

    @property
    def stream_seed(self) -> int:
        return self._stream_seed

    @property
    def draw_count(self) -> int:
        """本流已抽取次数（底层出口调用数），可用于定位"第几次抽取导致了这条事件"。"""

        return self._draw_count

    # —— 底层出口：唯一的两处计数点 ——

    def random(self) -> float:  # noqa: D102 - 语义同 random.Random.random
        self._draw_count += 1
        return super().random()

    def getrandbits(self, k: int) -> int:  # noqa: D102
        self._draw_count += 1
        return super().getrandbits(k)

    # —— 元数据 ——

    def event_metadata(self) -> dict[str, Any]:
        """S2-T6 给 stochastic 事件打标用（spec §4.5 "seed or deterministic random
        stream identifier"）。``draw_index`` 是当前已抽取次数，便于把一条事件对回
        回放时的同一次抽取。"""

        return {
            "rng_stream": self._name,
            "seed": self._run_seed,
            "stream_seed": self._stream_seed,
            "draw_index": self._draw_count,
        }

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (
            f"SimStream(name={self._name!r}, run_seed={self._run_seed}, "
            f"draws={self._draw_count})"
        )

    # random.Random 定义了 __reduce__/getstate 以支持 pickle；子流带自有身份字段，
    # 直接 pickle 会丢 name/seed。可复现性靠 (seed, name) 重建而非 pickle 状态，
    # 因此明确禁止序列化，避免有人 pickle 出一个"半个身份"的流。
    def __reduce__(self):  # pragma: no cover - 防御性
        raise TypeError("SimStream is not picklable; rebuild it from (seed, stream name)")


class SimRandom:
    """一个 run 的唯一随机源：一个 seed，多条命名子流。

    用法::

        rng = SimRandom(scenario.seed)          # seed=None 则自动生成并记录
        noise = rng.stream(RngStream.ENV_NOISE) # 每个消费者拿自己的流
        value += noise.gauss(0.0, 0.2)
        run_json["rng"] = rng.metadata()        # seed + 被抽取过的流
    """

    def __init__(self, seed: int | None = None) -> None:
        self._seed = new_seed() if seed is None else validate_seed(seed)
        self._streams: dict[str, SimStream] = {}

    # —— 身份 ——

    @property
    def seed(self) -> int:
        return self._seed

    # —— 子流访问 ——

    def stream(self, name: Any) -> SimStream:
        """取（必要时创建）命名子流。同名恒返回同一实例——否则每次调用都会从头
        重放同一批数字，随机性看着有、其实是常量。"""

        stream_name = normalize_stream_name(name)
        stream = self._streams.get(stream_name)
        if stream is None:
            stream = SimStream(stream_name, self._seed)
            self._streams[stream_name] = stream
        return stream

    # 语法糖：rng["env_noise"] 等价于 rng.stream("env_noise")。
    __getitem__ = stream

    def __contains__(self, name: Any) -> bool:
        try:
            return normalize_stream_name(name) in self._streams
        except (TypeError, ValueError):
            return False

    def __iter__(self) -> Iterator[SimStream]:
        return iter(self._streams.values())

    def known_streams(self) -> tuple[str, ...]:
        """已被申请过的流名（含申请但未抽取的），按名字排序。"""

        return tuple(sorted(self._streams))

    def drawn_streams(self) -> tuple[str, ...]:
        """**真正抽取过**的流名，按名字排序。

        与 :meth:`known_streams` 区分：只申请不抽取的流不进 run 元数据，否则
        "这个 run 用了哪些随机源"会被没消费的接线噪声污染。
        """

        return tuple(sorted(n for n, s in self._streams.items() if s.draw_count))

    def draw_counts(self) -> dict[str, int]:
        """流名 → 抽取次数（只含抽取过的流）。"""

        return {n: self._streams[n].draw_count for n in self.drawn_streams()}

    # —— run 元数据（§11） ——

    def metadata(self) -> dict[str, Any]:
        """写进 run.json / 事件工件的纯 JSON 元数据。

        只含 seed、算法版本与"抽取过的流及次数"——**不含**随机数生成器内部状态：
        复现靠 (seed, 流名) 重算，不靠状态快照，这样元数据小、可读、可 diff。
        """

        return {
            "version": METADATA_VERSION,
            "seed": self._seed,
            "streams": self.draw_counts(),
        }

    @classmethod
    def from_metadata(cls, metadata: Mapping[str, Any]) -> "SimRandom":
        """由 :meth:`metadata` 的产物重建（seed 往返）。

        重建的是一个**干净**的 run：流按需重新派生，序列与原 run 逐值一致。
        遇到未来版本的元数据直接报错，绝不用旧算法静默错误回放。
        """

        version = metadata.get("version", METADATA_VERSION)
        if version != METADATA_VERSION:
            raise ValueError(
                f"unsupported rng metadata version {version!r} (expected {METADATA_VERSION})"
            )
        if "seed" not in metadata:
            raise ValueError("rng metadata is missing required field 'seed'")
        return cls(validate_seed(metadata["seed"]))

    # —— 生命周期 ——

    def reset(self, seed: int | None = None) -> None:
        """重置为 run 起点（S2-T3 reset / 场景重跑）。

        丢弃所有子流与抽取记录；不传 seed 则沿用原 seed，于是重跑逐值复现。
        """

        if seed is not None:
            self._seed = validate_seed(seed)
        self._streams.clear()

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"SimRandom(seed={self._seed}, streams={list(self.known_streams())})"
