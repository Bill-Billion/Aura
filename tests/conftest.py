import pytest

from backend.engine.event_log import configure_runs_root


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def runs_root(tmp_path_factory):
    """把 run 工件根目录（默认 data/runs/）改到临时目录。

    autouse 是刻意的：任何构造 SimulationEngine 的测试都会开一个 run，进而落一份
    ``data/runs/{run_id}/``。不隔离的话整个测试套跑一遍就在仓库里堆几百个空 run 目录，
    而且两次测试之间会互相看见对方的工件（/api/runs 列表断言随即变成随机的）。
    """

    root = tmp_path_factory.mktemp("aura_runs")
    configure_runs_root(root)
    try:
        yield root
    finally:
        configure_runs_root(None)
