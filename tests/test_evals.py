import json

import pytest
from pydantic import ValidationError

from evals.compare import compare
from evals.graders import RUBRIC
from evals.providers import CallLimit, CappedProvider, ScriptedProvider
from evals.review import review
from evals.runner import ROOT, run
from evals.schema import Dataset
from reading_agent.agents.model_router import ModelCatalog
from reading_agent.agents.provider import ModelRequest, ProviderError, Settings

DATA = ROOT / "evals/datasets/reading-v1.json"


async def test_all_offline_cases_have_evidence_and_do_not_load_credentials(tmp_path, monkeypatch):
    def forbidden():
        raise AssertionError("offline run must not read model configuration")

    monkeypatch.setattr("evals.runner.load_catalog", forbidden)
    output, summary = await run(DATA, tmp_path)
    assert summary["counts"] == {"passed": 40} and summary["real_calls"] == 0
    assert summary["quality_acceptance"] == "not_evaluated_pending_human_review"
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert (
        manifest["dataset_sha256"]
        and manifest["source_sha256"]
        and manifest["as_of"].endswith("+08:00")
    )
    rows = [
        json.loads(line)
        for line in (output / "results.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(rows) == 40 and all(row["checks"] for row in rows)
    assert all(row["mode"] == "scripted_contract_only" for row in rows)
    assert json.loads((output / "reviews-template.json").read_text()) == []
    assert "text_delta" in next(row for row in rows if row["id"] == "tools-cancel")["event_types"]
    assert next(row for row in rows if row["id"] == "tools-discover-write")["footprints"][
        "note.txt"
    ]["exists"]


async def test_mutated_reference_fails_grader_instead_of_passing_everything(tmp_path):
    data = json.loads(DATA.read_text(encoding="utf-8"))
    target = next(case for case in data["cases"] if case["id"] == "tools-discover-write")
    target["expected"]["files"]["note.txt"] = "wrong expected content"
    data["cases"] = [target]
    path = tmp_path / "mutated.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    output, summary = await run(path, tmp_path / "report")
    assert summary["counts"] == {"failed": 1}
    row = json.loads((output / "tools-discover-write-1.json").read_text(encoding="utf-8"))
    assert not next(check for check in row["checks"] if check["name"] == "file:note.txt")["passed"]
    target["expected"]["files"]["note.txt"] = "hello"
    path.write_text(json.dumps(data), encoding="utf-8")
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    (fixtures / "topics.json").write_bytes((ROOT / "evals/fixtures/topics.json").read_bytes())
    scripts = json.loads((ROOT / "evals/fixtures/scripts.json").read_text(encoding="utf-8"))
    scripts[target["id"]].insert(
        -1,
        {
            "tool": "write_file",
            "arguments": {
                "path": "unexpected.txt",
                "content": "side effect",
                "expected_sha256": None,
            },
        },
    )
    (fixtures / "scripts.json").write_text(json.dumps(scripts), encoding="utf-8")
    extra, summary = await run(path, tmp_path / "extra", fixtures=fixtures)
    assert summary["counts"] == {"failed": 1}
    row = json.loads((extra / "tools-discover-write-1.json").read_text(encoding="utf-8"))
    assert not next(
        check for check in row["checks"] if check["name"] == "no_unexpected_file_writes"
    )["passed"]


async def test_repeat_and_comparison_reject_different_inputs(tmp_path):
    a, _ = await run(DATA, tmp_path / "a", ids=["route-irrelevant"], repeat=2)
    b, _ = await run(DATA, tmp_path / "b", ids=["route-irrelevant"], repeat=2)
    assert compare(a, b)["regressions"] == 0
    manifest = json.loads((b / "manifest.json").read_text())
    manifest["fixtures_sha256"] = "changed"
    (b / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="incomparable"):
        compare(a, b)


async def test_live_shared_call_cap_and_secret_free_manifest(tmp_path, monkeypatch):
    catalog = ModelCatalog(
        default="test",
        profiles={
            "test": Settings(
                api_key="secret-never-in-report",
                model="test-model",
                base_url="https://fixture.test",
            )
        },
    )
    monkeypatch.setattr("evals.runner.load_catalog", lambda: catalog)
    scripts = json.loads((ROOT / "evals/fixtures/scripts.json").read_text(encoding="utf-8"))

    class Provider(ScriptedProvider):
        def __init__(self, settings, client):
            super().__init__(scripts["route-irrelevant"] * 3)

    monkeypatch.setattr("reading_agent.agents.model_router.ChatProvider", Provider)
    output, summary = await run(
        DATA, tmp_path, live=True, repeat=2, maximum_calls=1, ids=["route-irrelevant"]
    )
    assert summary["real_calls"] == 1 and summary["counts"] == {"passed": 1, "skipped": 1}
    assert "secret-never-in-report" not in (output / "manifest.json").read_text()
    template = json.loads((output / "reviews-template.json").read_text())
    assert len(template) == 1 and all(score is None for score in template[0]["scores"].values())
    ratings = tmp_path / "ratings.json"
    ratings.write_text(json.dumps(template))
    with pytest.raises(ValueError, match="integer"):
        review(output, ratings)
    template[0].update(
        scores={dimension: 2 for dimension in RUBRIC},
        reviewer="test reviewer",
        evidence="原文未涉及目标Topic，零归属合理。",
    )
    ratings.write_text(json.dumps(template))
    assert review(output, ratings)["status"] == "complete"
    template[0]["output_sha256"] = "stale"
    ratings.write_text(json.dumps(template))
    with pytest.raises(ValueError, match="match"):
        review(output, ratings)


async def test_global_cap_applies_before_provider_request():
    limit = CallLimit(1)
    provider = CappedProvider(
        ScriptedProvider([{"final": {"answer": "one"}}, {"final": {"answer": "two"}}]), limit
    )
    request = ModelRequest([], [], {}, 100)
    assert [event async for event in provider.stream(request)]
    with pytest.raises(ProviderError, match="eval_call_budget_exhausted"):
        [event async for event in provider.stream(request)]
    assert limit.used == 1


def test_dataset_rejects_duplicate_ids_and_naive_time():
    raw = json.loads(DATA.read_text(encoding="utf-8"))
    raw["cases"].append(raw["cases"][0])
    with pytest.raises(ValidationError, match="duplicate"):
        Dataset.model_validate_json(json.dumps(raw))
    raw["cases"] = raw["cases"][:1]
    raw["as_of"] = "2026-11-08"
    with pytest.raises(ValidationError, match="timezone"):
        Dataset.model_validate_json(json.dumps(raw))


async def test_unknown_case_does_not_silently_run_default_suite(tmp_path):
    with pytest.raises(ValueError, match="selection"):
        await run(DATA, tmp_path, ids=["typo"])


async def test_report_contains_replayable_inputs(tmp_path):
    output, _ = await run(DATA, tmp_path, ids=["route-irrelevant"])
    replay, _ = await run(
        output / "inputs/dataset.json",
        tmp_path / "replay",
        ids=["route-irrelevant"],
        fixtures=output / "inputs",
    )
    assert compare(output, replay)["regressions"] == 0
    assert (output / "inputs/dataset.json").read_bytes() == DATA.read_bytes()


async def test_scripted_report_cannot_be_accepted_as_human_live_quality(tmp_path):
    output, _ = await run(DATA, tmp_path, ids=["route-irrelevant"])
    row = json.loads((output / "route-irrelevant-1.json").read_text(encoding="utf-8"))
    ratings = tmp_path / "ratings.json"
    ratings.write_text(
        json.dumps([{key: row[key] for key in ("run_id", "id", "repeat", "output_sha256")}])
    )
    with pytest.raises(ValueError, match="real model"):
        review(output, ratings)
