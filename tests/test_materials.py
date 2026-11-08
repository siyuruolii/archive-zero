import sqlite3

import httpx
import pytest
from conftest import ScriptedProvider, route_item, route_json

from reading_agent.agents.models import RouteFinal
from reading_agent.infrastructure.fetching import FeedItem, Fetcher, parse_feed
from reading_agent.services.materials import MaterialService
from reading_agent.storage.database import Conflict


class Feeds:
    async def feed(self, url):
        return [FeedItem("one", "工具", "https://example.test/1", "工具提示", "2020-01-01")]

    async def body(self, url):
        return "工具由模型提出请求，程序检查权限后执行。"


def item(tid, tag, **changes):
    return route_item(tid, [tag]) | {"needs_deep_read": False} | changes


async def prepare(setup):
    store, tid, tag = setup
    service = MaterialService(store, Feeds(), ScriptedProvider())
    source = await service.add_source("RSS", "https://example.test/feed", [tid])
    mids = service.ingest(source["id"], await service.fetcher.feed(""))
    return service, source["id"], mids[0], tid, tag


async def test_initialization_none_does_not_create_seen_watermark(setup):
    service, sid, mid, tid, tag = await prepare(setup)
    assert len(service.store.materials()) == 1
    assert service.ingest(sid, await service.fetcher.feed("")) == []
    assert service.store.materials()[0]["published_at"] == "2020-01-01"
    assert service.store.inbox(tid) == []


async def test_duplicate_source_does_not_enable_or_initialize(setup):
    service, sid, mid, tid, tag = await prepare(setup)
    service.source_state(sid, enabled=False)
    result = await service.add_source("new", "https://example.test/feed", [tid], "model")
    assert result["id"] == sid and not result["created"]
    assert not service.store.one("SELECT * FROM sources WHERE id=?", (sid,))["is_enabled"]
    service.source_state(sid, removed=True)
    new = await service.add_source("new", "https://example.test/feed")
    assert new["id"] != sid


async def test_links_do_not_backfill_and_reuse_identity(setup):
    service, sid, mid, tid, tag = await prepare(setup)
    before = service.store.one("SELECT * FROM topic_sources")
    service.store.link(tid, sid, False)
    service.store.link(tid, sid, True)
    assert service.store.one("SELECT * FROM topic_sources")["id"] == before["id"]
    assert not service.store.inbox(tid)


async def test_atomic_route_and_user_modifications_preserved(setup):
    service, sid, mid, tid, tag = await prepare(setup)
    other = service.store.topic("其他")
    service.store.link(other, sid)
    bad = RouteFinal.model_validate_json(route_json([item(tid, tag), item(other, "bad-tag")]))
    with pytest.raises(Conflict):
        service.commit_route(mid, bad)
    assert not service.store.inbox(tid)
    result = RouteFinal.model_validate_json(route_json([item(tid, tag)]))
    tm = service.commit_route(mid, result)[0]
    service.store.feedback(tm, is_read=True, interest="uninterested", tag_ids=[])
    assert service.commit_route(mid, result) == []
    row = service.store.inbox(tid)[0]
    assert row["is_read"] and row["interest"] == "uninterested" and row["tag_ids"] == []
    assert not service.store.inbox(tid, recommended=True)


async def test_scope_fk_and_material_delete_guard(setup):
    service, sid, mid, tid, tag = await prepare(setup)
    tm = service.commit_route(mid, RouteFinal.model_validate_json(route_json([item(tid, tag)])))[0]
    with pytest.raises(Conflict):
        service.store.delete("materials", mid)
    other = service.store.topic("other")
    other_tag = service.store.tag(other, "other")
    with service.store.transaction() as db:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO material_tags VALUES(?,?,?)", (tm, other_tag, tid))
    service.store.delete("topic_materials", tm)
    assert service.ingest(sid, await service.fetcher.feed("")) == []
    new = service.commit_route(mid, RouteFinal.model_validate_json(route_json([item(tid, tag)])))[0]
    assert new != tm


async def test_tag_changes_do_not_cancel_pending_but_definition_delete_does(setup):
    service, sid, mid, tid, tag = await prepare(setup)
    tm = service.commit_route(
        mid, RouteFinal.model_validate_json(route_json([item(tid, tag, needs_deep_read=True)]))
    )[0]
    service.store.feedback(tm, tag_ids=[])
    assert service.store.inbox(tid)[0]["deep_summary_status"] == "pending"
    service.store.feedback(tm, tag_ids=[tag])
    service.store.delete("tags", tag)
    assert service.store.inbox(tid)[0]["deep_summary_status"] == "cancelled"


async def test_route_real_storage_with_scripted_model(setup):
    service, sid, mid, tid, tag = await prepare(setup)
    service.provider = ScriptedProvider(route_json([item(tid, tag)]))
    result = await service.route(mid)
    assert result["status"] == "succeeded"
    assert service.store.inbox(tid)[0]["short_summary"]
    assert service.store.one("SELECT * FROM materials")["routing_status"] == "succeeded"


async def test_body_persistence_only_for_discussion(setup):
    service, sid, mid, tid, tag = await prepare(setup)
    assert await service.load_body(mid)
    assert not service.store.all("SELECT * FROM material_bodies")
    await service.load_body(mid, persist=True)
    assert service.store.all("SELECT * FROM material_bodies")


def test_rss_atom_and_untrusted_xml():
    rss = (
        b"<rss><channel><item><guid>x</guid><title>hello</title><link>/x</link>"
        b"<description>&lt;b&gt;body&lt;/b&gt;</description></item></channel></rss>"
    )
    assert parse_feed(rss, "https://example.test/feed")[0].summary == "body"
    atom = (
        b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>x</id>'
        b'<title>A</title><link href="/x"/></entry></feed>'
    )
    assert parse_feed(atom, "https://example.test/feed")[0].url == "https://example.test/x"
    with pytest.raises(ValueError):
        parse_feed(b"<!DOCTYPE rss><rss/>", "https://example.test")


async def test_http_body_strips_scripts():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200, text="<html><script>do evil()</script><main>真实正文</main></html>"
            )
        )
    ) as client:
        assert await Fetcher(client).body("https://example.test/body") == "真实正文"
