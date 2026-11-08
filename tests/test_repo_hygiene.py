"""仓库规则的正例与故障注入，确保拒绝坏地图和逆向依赖。"""

from pathlib import Path

from scripts.check_repo import check_docs, check_imports


def minimal_map(root):
    (root / "docs").mkdir()
    (root / "evals").mkdir()
    (root / "README.md").write_text("[Eval](evals/README.md)", encoding="utf-8")
    (root / "AGENTS.md").write_text("[Readme](README.md)", encoding="utf-8")
    (root / "evals/README.md").write_text("# Eval", encoding="utf-8")


def test_current_repository_map_and_imports():
    root = Path(__file__).resolve().parents[1]
    assert check_docs(root) == []
    assert check_imports(root) == []


def test_broken_link_and_orphan_are_reported(tmp_path):
    minimal_map(tmp_path)
    (tmp_path / "docs/lost.md").write_text("[gone](missing.md)", encoding="utf-8")
    errors = check_docs(tmp_path)
    assert any("broken local link" in error for error in errors)
    assert any("orphan document" in error for error in errors)
    (tmp_path / "docs/interview.md").write_text("personal notes", encoding="utf-8")
    assert any("personal preparation" in error for error in check_docs(tmp_path))


def test_code_examples_and_external_links_are_not_local_targets(tmp_path):
    minimal_map(tmp_path)
    with (tmp_path / "README.md").open("a", encoding="utf-8") as file:
        file.write("\n[web](https://example.test/x)\n```md\n[sample](missing.md)\n```")
    assert check_docs(tmp_path) == []


def test_large_entry_and_escaping_link_are_rejected(tmp_path):
    minimal_map(tmp_path)
    (tmp_path / "AGENTS.md").write_text("[outside](../outside.md)\n" * 101, encoding="utf-8")
    errors = check_docs(tmp_path)
    assert any("100 lines" in error for error in errors)
    assert any("escapes repository" in error for error in errors)


def test_absolute_and_relative_reverse_imports_are_rejected(tmp_path):
    agents = tmp_path / "src/reading_agent/agents"
    agents.mkdir(parents=True)
    (agents / "bad.py").write_text(
        "from reading_agent.services import memory\nfrom ..interfaces import api\n",
        encoding="utf-8",
    )
    errors = check_imports(tmp_path)
    assert len(errors) == 2
    assert all("cannot import" in error for error in errors)
    services = tmp_path / "src/reading_agent/services"
    services.mkdir()
    (services / "bad.py").write_text(
        "from reading_agent.agents.loop import AgentLoop\n", encoding="utf-8"
    )
    assert any("must use Harness" in error for error in check_imports(tmp_path))


def test_configuration_wiring_exception_is_limited_to_existing_contracts(tmp_path):
    infrastructure = tmp_path / "src/reading_agent/infrastructure"
    infrastructure.mkdir(parents=True)
    (infrastructure / "configuration.py").write_text(
        "from reading_agent.agents.provider import Settings\n"
        "from reading_agent.services import materials\n",
        encoding="utf-8",
    )
    (infrastructure / "fetching.py").write_text(
        "from reading_agent.agents.provider import Settings\n", encoding="utf-8"
    )
    errors = check_imports(tmp_path)
    assert len(errors) == 2
    assert any("materials" in error for error in errors)
    assert any("fetching.py" in error for error in errors)
