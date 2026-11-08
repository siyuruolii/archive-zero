import json

import pytest

from reading_agent.agents.provider import ProviderEvent, Turn
from reading_agent.agents.sample import sample_corpus


def pytest_addoption(parser):
    parser.addoption(
        "--run-live",
        action="store_true",
        default=False,
        help="显式启用会消耗模型额度的真实调用测试",
    )


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--run-live"):
        for item in items:
            if item.get_closest_marker("live"):
                item.add_marker(pytest.mark.skip(reason="未启用 --run-live；未请求真实模型"))


class ScriptedProvider:
    """只用于测试的模型替身。生产入口绝不使用它。"""

    def __init__(self, *steps):
        self.steps = list(steps)
        self.requests = []

    async def stream(self, request):
        self.requests.append(request)
        if not self.steps:
            raise AssertionError("unexpected model call")
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        if isinstance(step, str):
            yield ProviderEvent("text", text=step)
            step = Turn(content=step)
        yield ProviderEvent("turn", turn=step)


def route_json(routes=None, status="succeeded", reason="基于实际资料的判断"):
    return json.dumps(
        {"status": status, "reason": reason, "routes": routes or []}, ensure_ascii=False
    )


def route_item(topic_id="learn", tag_ids=None):
    return {
        "topic_id": topic_id,
        "tag_ids": tag_ids or [],
        "short_summary": "模型提出请求，程序检查权限并执行工具。",
        "match_reason": "与工具学习相关",
        "is_recommended": True,
        "recommendation_reason": "包含分工解释",
        "needs_deep_read": True,
        "deep_read_guideline": "解释权限边界",
    }


@pytest.fixture
def corpus():
    return sample_corpus()


@pytest.fixture
def setup(tmp_path):
    from reading_agent.storage.database import Store

    store = Store(tmp_path / "app.db")
    tid = store.topic("学习", "Python Agent")
    tag = store.tag(tid, "工具")
    return store, tid, tag


@pytest.fixture
async def state(tmp_path):
    from test_materials import Feeds

    from reading_agent.agents.models import RouteFinal
    from reading_agent.services.materials import MaterialService
    from reading_agent.services.memory import Memory
    from reading_agent.services.sessions import Sessions
    from reading_agent.storage.database import Store

    store = Store(tmp_path / "test.db")
    tid = store.topic("学习")
    tag = store.tag(tid, "工具")
    service = MaterialService(store, Feeds(), ScriptedProvider())
    src = await service.add_source("rss", "https://example.test/feed", [tid])
    mid = service.ingest(src["id"], await service.fetcher.feed(""))[0]
    item = route_item(tid, [tag]) | {"needs_deep_read": False}
    tm = service.commit_route(mid, RouteFinal.model_validate_json(route_json([item])))[0]
    sessions = Sessions(store)
    created = sessions.create(tm)
    memory = Memory(store, service.provider)
    return store, service, sessions, memory, created, tid, tm
