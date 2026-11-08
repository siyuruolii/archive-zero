"""链接目录为派生产物；MD 文档是独立且可迁移的权威数据。"""

import json
import os
import re

import pytest

from reading_agent.infrastructure.locking import FileLock
from reading_agent.services.memory import Memory
from reading_agent.storage.database import Conflict

FOLDERS = {"user_reading": "user-reading", "agent_reading": "agent-reading"}


def catalog_path(memory, category):
    return memory.directory / FOLDERS[category] / "memory.md"


def linked_paths(catalog):
    text = catalog.read_text(encoding="utf-8")
    return [
        catalog.parent / link
        for link in re.findall(r"(?<!\\)\]\((entries/[a-zA-Z0-9_-]+\.md)\)", text)
    ]


@pytest.mark.parametrize("category", list(FOLDERS))
async def test_catalog_links_to_frontmatter_documents_without_copying_content(state, category):
    _, _, _, memory, _, tid, _ = state
    mid = memory.add(
        "私有正文不应复制到链接目录",
        topic_id=tid,
        kind=category,
        details={
            "title": "材料 [参考](https://example.test)\n<script>内容</script>",
            "action": "read",
            "complete": True,
        },
    )
    catalog = catalog_path(memory, category)
    path = catalog.parent / "entries" / (mid + ".md")
    assert linked_paths(catalog) == [path]
    text = catalog.read_text(encoding="utf-8")
    assert "私有正文" not in text and "<script>" not in text
    assert "\\[参考\\]" in text and "&lt;script&gt;" in text
    header, content = path.read_text(encoding="utf-8")[4:].split("\n---\n\n", 1)
    metadata = json.loads(header)
    assert metadata["id"] == mid and metadata["category"] == category
    assert content.strip() == "私有正文不应复制到链接目录"
    assert not (catalog.parent / (mid + ".md")).exists()


@pytest.mark.parametrize("category", list(FOLDERS))
async def test_correction_and_delete_update_catalog_without_resurrecting_old_entry(state, category):
    _, _, _, memory, _, tid, _ = state
    old = memory.add("旧记录", topic_id=tid, kind=category)
    new = memory.correct(old, "新记录")
    catalog = catalog_path(memory, category)
    assert [p.stem for p in linked_paths(catalog)] == [new]
    memory.delete(new)
    assert linked_paths(catalog) == []
    assert memory.list(tid, category=category) == []
    assert (catalog.parent / "entries" / (old + ".md")).exists()
    reopened = Memory(memory.store, None)
    assert reopened.list(tid, category=category) == []
    assert linked_paths(catalog) == []


@pytest.mark.parametrize("damage", ["missing", "wrong_links", "invalid_utf8"])
async def test_catalog_rebuild_ignores_missing_or_corrupt_catalog_content(state, damage):
    _, _, _, memory, _, tid, _ = state
    mid = memory.add("应当保留的记忆", topic_id=tid, kind="agent_reading")
    catalog = catalog_path(memory, "agent_reading")
    if damage == "missing":
        catalog.unlink()
    elif damage == "wrong_links":
        catalog.write_text("- [假条目](../../outside.md)\n", encoding="utf-8")
    else:
        catalog.write_bytes(b"\xff\xfe\x80")
    assert memory.rebuild() == 1
    assert [p.stem for p in linked_paths(catalog)] == [mid]
    assert memory.list(tid, category="agent_reading")[0]["content"] == "应当保留的记忆"


@pytest.mark.parametrize("duplicate", [False, True])
async def test_flat_layout_migrates_and_identical_duplicates_do_not_duplicate_memory(
    state, duplicate
):
    _, _, _, memory, _, tid, _ = state
    ids = []
    originals = {}
    for category in FOLDERS:
        mid = memory.add("旧布局 " + category, topic_id=tid, kind=category)
        catalog = catalog_path(memory, category)
        path = catalog.parent / "entries" / (mid + ".md")
        originals[mid] = path.read_bytes()
        flat = catalog.parent / path.name
        if duplicate:
            flat.write_bytes(path.read_bytes())
        else:
            os.replace(path, flat)
        ids.append((category, mid, flat, path))
    reopened = Memory(memory.store, None)
    for category, mid, flat, path in ids:
        assert not flat.exists() and path.read_bytes() == originals[mid]
        assert [p.stem for p in linked_paths(catalog_path(reopened, category))] == [mid]
    assert reopened.rebuild() == 2


async def test_conflicting_duplicates_stop_migration_without_overwriting_either_copy(state):
    _, _, _, memory, _, tid, _ = state
    mid = memory.add("权威记录", topic_id=tid, kind="agent_reading")
    catalog = catalog_path(memory, "agent_reading")
    nested = catalog.parent / "entries" / (mid + ".md")
    original = nested.read_bytes()
    flat = catalog.parent / nested.name
    flat.write_text(
        nested.read_text(encoding="utf-8").replace("权威记录", "冲突记录"), encoding="utf-8"
    )
    with pytest.raises(Conflict, match="conflicting"):
        memory.rebuild()
    assert nested.read_bytes() == original and "冲突记录" in flat.read_text(encoding="utf-8")


async def test_interrupted_layout_migration_can_resume_without_losing_records(state, monkeypatch):
    _, _, _, memory, _, tid, _ = state
    mids = [memory.add("旧记录" + str(i), topic_id=tid, kind="agent_reading") for i in range(2)]
    directory = catalog_path(memory, "agent_reading").parent
    for mid in mids:
        os.replace(directory / "entries" / (mid + ".md"), directory / (mid + ".md"))
    original_replace = os.replace
    calls = 0

    def interrupt(source, target):
        nonlocal calls
        if source.parent == directory:
            calls += 1
            if calls == 2:
                raise OSError("simulated migration interruption")
        return original_replace(source, target)

    with monkeypatch.context() as patch:
        patch.setattr("reading_agent.services.memory.os.replace", interrupt)
        with pytest.raises(OSError, match="interruption"):
            memory.rebuild()
    assert memory.rebuild() == 2
    assert {p.stem for p in linked_paths(directory / "memory.md")} == set(mids)
    assert not any((directory / (mid + ".md")).exists() for mid in mids)


async def test_catalog_write_failure_preserves_committed_document_and_rebuild_repairs(
    state, monkeypatch
):
    _, _, _, memory, _, tid, _ = state
    catalog = catalog_path(memory, "agent_reading")
    original_catalog = catalog.read_bytes()
    original_replace = os.replace

    def fail_index(source, target):
        if target == catalog:
            raise OSError("simulated catalogue failure")
        return original_replace(source, target)

    with monkeypatch.context() as patch:
        patch.setattr("reading_agent.services.memory.os.replace", fail_index)
        with pytest.raises(OSError, match="catalogue"):
            memory.add("记忆文件已提交", topic_id=tid, kind="agent_reading")
    files = list((catalog.parent / "entries").glob("*.md"))
    assert len(files) == 1 and "记忆文件已提交" in files[0].read_text(encoding="utf-8")
    assert catalog.read_bytes() == original_catalog
    assert not list(memory.directory.rglob("*.tmp"))
    assert memory.rebuild() == 1 and linked_paths(catalog) == files


async def test_single_memory_lock_covers_both_catalogs_and_entry_writes(state):
    _, _, _, memory, _, tid, _ = state
    originals = {category: catalog_path(memory, category).read_bytes() for category in FOLDERS}
    with FileLock(memory.directory / ".memory.lock"):
        for category in FOLDERS:
            with pytest.raises(Conflict):
                memory.add("不能绕过文件锁", topic_id=tid, kind=category)
    assert not (memory.directory / ".write.lock").exists()
    for category in FOLDERS:
        assert catalog_path(memory, category).read_bytes() == originals[category]
        assert linked_paths(catalog_path(memory, category)) == []


async def test_legacy_memory_id_named_memory_is_not_overwritten_by_catalog(state):
    _, _, _, memory, _, tid, _ = state
    mid = memory.add("保留特殊旧ID", topic_id=tid, kind="agent_reading")
    catalog = catalog_path(memory, "agent_reading")
    path = catalog.parent / "entries" / (mid + ".md")
    text = path.read_text(encoding="utf-8").replace(mid, "memory")
    path.unlink()
    catalog.write_text(text, encoding="utf-8")
    memory.rebuild()
    assert [p.name for p in linked_paths(catalog)] == ["memory.md"]
    assert linked_paths(catalog)[0].parent.name == "entries"
    assert memory.list(tid, category="agent_reading")[0]["content"] == "保留特殊旧ID"
