"""S2-T1：单一来源 seed 化 RNG 测试（spec §4.5 / §11 可复现契约）。

这些断言是整个 S2 可复现性的地基，钉死四件事：
1. 同 seed + 同流名 → 逐值一致的序列（跨进程、跨 SimRandom 实例）；
2. 流之间互相独立——消费 A 流不会移动 B 流的游标（否则"加一个新消费者"就会
   悄悄改变旧场景的回放结果，可复现性名存实亡）；
3. **新增流不扰动既有流**：派生方式必须是"按名字散列"，绝不能是"按申请顺序 spawn"；
4. seed 与被抽取过的流名可序列化进 run 元数据并原样往返（§4.5 的
   "seed or deterministic random stream identifier"、§11 run 元数据 seed 字段）。
"""

from __future__ import annotations

import json
import random

import pytest

from backend.engine.rng import (
    MAX_SEED,
    METADATA_VERSION,
    RngStream,
    SimRandom,
    SimStream,
    derive_stream_seed,
)


def _draw(stream: SimStream, n: int = 8) -> list[float]:
    return [stream.random() for _ in range(n)]


# —— 1. 同 seed 同流 → 同序列 ——


def test_same_seed_same_sequence_per_stream():
    a = SimRandom(1001)
    b = SimRandom(1001)
    for name in (RngStream.USER_SIM, RngStream.ENV_NOISE, RngStream.STOCHASTIC_EVENTS):
        assert _draw(a.stream(name)) == _draw(b.stream(name))


def test_different_seed_changes_every_stream():
    a = SimRandom(1001)
    b = SimRandom(1002)
    for name in (RngStream.USER_SIM, RngStream.ENV_NOISE):
        assert _draw(a.stream(name)) != _draw(b.stream(name))


def test_stream_accessor_is_idempotent_and_keeps_advancing():
    rng = SimRandom(7)
    first = rng.stream(RngStream.USER_SIM)
    second = rng.stream("user_sim")
    # 同名必须拿到同一个实例，否则消费者每次调用都会从头重放同一批数字。
    assert first is second
    seq = _draw(first, 4) + _draw(second, 4)
    assert seq == _draw(SimRandom(7).stream(RngStream.USER_SIM), 8)


# —— 2. 流独立 ——


def test_streams_are_independent():
    baseline = _draw(SimRandom(42).stream(RngStream.ENV_NOISE))

    rng = SimRandom(42)
    for _ in range(500):  # 大量消费 A 流
        rng.stream(RngStream.USER_SIM).random()
    assert _draw(rng.stream(RngStream.ENV_NOISE)) == baseline


def test_distinct_streams_have_distinct_sequences():
    rng = SimRandom(42)
    a = _draw(rng.stream(RngStream.USER_SIM))
    b = _draw(rng.stream(RngStream.ENV_NOISE))
    assert a != b
    assert derive_stream_seed(42, "user_sim") != derive_stream_seed(42, "env_noise")


# —— 3. 新增流不扰动既有流（本任务的核心不变式）——


def test_adding_new_stream_does_not_perturb_existing_streams():
    baseline_env = _draw(SimRandom(2026).stream(RngStream.ENV_NOISE))

    rng = SimRandom(2026)
    # 模拟"S3 又接进来一个新随机消费者"：先申请并消费若干新流。
    for extra in ("s3_arbiter_tiebreak", "s4_fault_injection", "future_consumer"):
        for _ in range(10):
            rng.stream(extra).random()
    assert _draw(rng.stream(RngStream.ENV_NOISE)) == baseline_env


def test_stream_seed_depends_only_on_seed_and_name():
    # 派生必须是纯函数（无申请顺序、无全局状态），跨进程可重算。
    assert derive_stream_seed(5, "env_noise") == derive_stream_seed(5, "env_noise")
    assert derive_stream_seed(5, "env_noise") != derive_stream_seed(6, "env_noise")
    assert 0 <= derive_stream_seed(5, "env_noise") <= MAX_SEED


# —— 4. 元数据可序列化 / 往返 ——


def test_stream_id_metadata_serializable():
    rng = SimRandom(1001)
    rng.stream(RngStream.STOCHASTIC_EVENTS).random()

    meta = rng.metadata()
    dumped = json.dumps(meta)  # 必须是纯 JSON 值（进 run.json / JSONL 事件工件）
    assert json.loads(dumped) == meta
    assert meta["seed"] == 1001
    assert meta["streams"]["stochastic_events"] == 1


def test_metadata_round_trips_seed_and_replays_sequences():
    rng = SimRandom(20260721)
    before = _draw(rng.stream(RngStream.OBSERVATION_NOISE))

    restored = SimRandom.from_metadata(json.loads(json.dumps(rng.metadata())))
    assert restored.seed == rng.seed
    assert _draw(restored.stream(RngStream.OBSERVATION_NOISE)) == before


def test_metadata_records_only_streams_actually_drawn():
    rng = SimRandom(3)
    rng.stream(RngStream.USER_SIM)  # 只申请不抽取
    rng.stream(RngStream.ENV_NOISE).gauss(0.0, 1.0)

    streams = rng.metadata()["streams"]
    # gauss 等高层方法一次调用可能消耗多次底层抽取，故只断言"被记录且计数为正"，
    # 不把标准库的内部抽取次数写死进契约。
    assert set(streams) == {"env_noise"}
    assert streams["env_noise"] >= 1
    assert rng.drawn_streams() == ("env_noise",)
    assert rng.known_streams() == ("env_noise", "user_sim")


def test_stream_event_metadata_carries_rng_stream_and_seed():
    # S2-T6 给 stochastic 事件打标用的正是这份字段（spec §4.5 第五项元数据）。
    rng = SimRandom(88)
    stream = rng.stream(RngStream.STOCHASTIC_EVENTS)
    stream.random()
    meta = stream.event_metadata()

    assert meta["rng_stream"] == "stochastic_events"
    assert meta["seed"] == 88
    assert meta["draw_index"] == 1
    assert json.loads(json.dumps(meta)) == meta


# —— seed 契约 ——


def test_seed_is_generated_and_recorded_when_absent():
    rng = SimRandom()
    assert isinstance(rng.seed, int)
    assert 0 <= rng.seed <= MAX_SEED
    # 未显式给 seed 的 run 也必须能被复现：seed 已被记录，可原样重建。
    replay = SimRandom(rng.seed)
    assert _draw(replay.stream(RngStream.USER_SIM)) == _draw(rng.stream(RngStream.USER_SIM))


@pytest.mark.parametrize("bad_seed", [-1, MAX_SEED + 1, 1.5, "1001", True])
def test_invalid_seed_rejected(bad_seed):
    with pytest.raises((TypeError, ValueError)):
        SimRandom(bad_seed)


@pytest.mark.parametrize("bad_name", ["", "  ", "env noise", "env/noise", None, 3])
def test_invalid_stream_name_rejected(bad_name):
    rng = SimRandom(1)
    with pytest.raises((TypeError, ValueError)):
        rng.stream(bad_name)


def test_canonical_stream_names_cover_planned_consumers():
    assert {s.value for s in RngStream} >= {
        "user_sim",
        "env_noise",
        "stochastic_events",
        "observation_noise",
    }


# —— 黄金向量：跨进程 / 跨版本回放锚点 ——


def test_golden_vector_pins_the_derivation_algorithm():
    """派生算法一旦改变，所有历史 run 的回放结果就全变了。

    这条断言故意写死数值：改动 ``derive_stream_seed`` / ``_DERIVE_PERSON`` 必须
    在这里显式失败一次，并同步 ``METADATA_VERSION``，而不是让旧 run.json 被静默
    错误回放。数值来自 CPython Mersenne Twister + blake2b，两者跨进程跨版本稳定。
    """

    assert derive_stream_seed(1001, "env_noise") == 7427020052783229287

    stream = SimRandom(1001).stream(RngStream.ENV_NOISE)
    assert [round(stream.random(), 12) for _ in range(3)] == [
        0.322378258309,
        0.128644967135,
        0.837005724729,
    ]
    # 走 getrandbits 出口的高层方法同样被钉住。
    assert SimRandom(1001).stream(RngStream.USER_SIM).randint(0, 1000) == 275


def test_metadata_version_is_pinned():
    assert SimRandom(1).metadata()["version"] == METADATA_VERSION == 1


def test_from_metadata_rejects_unknown_version_and_missing_seed():
    with pytest.raises(ValueError):
        SimRandom.from_metadata({"version": METADATA_VERSION + 1, "seed": 1})
    with pytest.raises(ValueError):
        SimRandom.from_metadata({"version": METADATA_VERSION, "streams": {}})


# —— 与既有代码的接缝 / 无全局随机源 ——


def test_stream_is_drop_in_for_random_Random():
    # backend/simulators/effects.py 的 ``rng: random.Random | None`` 接缝必须能直接吃 SimStream。
    from backend.simulators.effects import generate_sensor_reading

    stream = SimRandom(11).stream(RngStream.OBSERVATION_NOISE)
    assert isinstance(stream, random.Random)
    first = generate_sensor_reading(24.5, rng=stream, noise=0.5)
    second = generate_sensor_reading(24.5, rng=SimRandom(11).stream(RngStream.OBSERVATION_NOISE), noise=0.5)
    assert first == second


def test_does_not_touch_global_random_state():
    random.seed(123)
    before = random.getstate()
    rng = SimRandom(999)
    for name in RngStream:
        rng.stream(name).random()
    assert random.getstate() == before


def test_simrandom_public_surface_is_exactly_what_is_used():
    """SimRandom 是可复现性的地基类：不留"文档里有、全仓库零调用"的方法。

    S2 评审删掉的 ``prewarm()`` 就是那一类——没有调用方也就没有测试，等到某一天
    真有人用上时，它的实现早已在无人看管下漂走。新增方法请连同调用方一起加进来。
    """

    surface = {name for name in vars(SimRandom) if not name.startswith("_")}
    assert surface == {
        "seed",
        "stream",
        "known_streams",
        "drawn_streams",
        "draw_counts",
        "metadata",
        "from_metadata",
        "reset",
    }


def test_reset_replays_the_same_run():
    rng = SimRandom(555)
    first = _draw(rng.stream(RngStream.STOCHASTIC_EVENTS))

    rng.reset()
    assert rng.drawn_streams() == ()
    assert _draw(rng.stream(RngStream.STOCHASTIC_EVENTS)) == first
