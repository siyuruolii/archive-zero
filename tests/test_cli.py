import json
from pathlib import Path

from reading_agent.agents.sample import sample_corpus
from reading_agent.cli import execute, parser
from reading_agent.infrastructure.configuration import data_dir


async def test_preview_does_not_need_credentials(monkeypatch, capsys):
    monkeypatch.delenv("AGENT_API_KEY", raising=False)
    monkeypatch.delenv("AGENT_DATA_DIR", raising=False)
    assert data_dir() == Path(__file__).resolve().parents[1] / ".dev-data"
    assert sample_corpus().materials["tools"].body
    code = await execute(parser().parse_args(["route", "--preview"]))
    assert code == 0
    messages = json.loads(capsys.readouterr().out)
    assert messages[0]["role"] == "system"


async def test_unconfigured_live_run_fails_without_secret_echo(monkeypatch, capsys):
    monkeypatch.setenv("AGENT_API_KEY", "a-secret-that-must-not-appear")
    monkeypatch.setenv("AGENT_MODEL", "replace-with-your-model")
    monkeypatch.setenv("AGENT_BASE_URL", "https://model.example/v1")
    code = await execute(parser().parse_args(["route"]))
    assert code == 2
    captured = capsys.readouterr()
    assert "a-secret" not in captured.err + captured.out
