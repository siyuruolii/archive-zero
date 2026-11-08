"""硬检查与人工质量量表分离；关键词仅辅助定位，不作为语义评分。"""

import hashlib
import json

RUBRIC = {
    "faithfulness": "0=核心事实错误；1=重要曲解；2=基本忠实但有瑕疵；3=事实与证据充分一致",
    "coverage": "0=未回答；1=遗漏核心要求；2=覆盖主要要求；3=覆盖关键点且重点清晰",
    "uncertainty": (
        "0=编造或隐瞒缺失；1=限制表达误导；2=指出主要限制；3=清楚区分事实、推测和未读范围"
    ),
    "relevance": "0=不相关；1=偏离用户目标；2=基本相关；3=对当前Topic和问题有直接帮助",
}


def grade(case, report, workspace):
    checks = []

    def check(name, passed, detail=""):
        checks.append({"name": name, "passed": bool(passed), "detail": detail})

    expected = case.expected
    check("terminal_status", report.status == expected.status, report.end_reason)
    if expected.end_reason:
        check("end_reason", report.end_reason == expected.end_reason, report.end_reason)
    routes = (report.result or {}).get("routes", [])
    if expected.topic_ids is not None:
        check("topic_set", {r["topic_id"] for r in routes} == set(expected.topic_ids))
    for topic, recommended in expected.recommended.items():
        rows = [r for r in routes if r["topic_id"] == topic]
        check(
            "recommendation:" + topic, len(rows) == 1 and rows[0]["is_recommended"] == recommended
        )
    if expected.must_read:
        check(
            "actual_reading_receipt",
            any(r["material_id"] == case.task.material_id for r in report.readings),
        )
    observed_errors = {tool["result"].get("error") for tool in report.tools}
    for code in expected.tool_errors:
        check("tool_error:" + code, code in observed_errors)
    check("model_call_budget", report.model_calls <= case.budget.max_model_calls)
    check("tool_call_budget", report.tool_calls <= case.budget.max_tool_calls)
    footprints = {}
    for relative, expected_text in expected.files.items():
        path = workspace.path(relative)
        actual = path.read_text(encoding="utf-8") if path.is_file() else None
        footprints[relative] = {
            "exists": actual is not None,
            "sha256": hashlib.sha256(actual.encode()).hexdigest() if actual is not None else None,
        }
        check("file:" + relative, actual == expected_text)
    if expected.files:
        actual_files = {
            path.relative_to(workspace.root).as_posix()
            for path in workspace.root.rglob("*")
            if path.is_file()
            and not any(part.startswith(".") for part in path.relative_to(workspace.root).parts)
        }
        allowed = {name for name, text in expected.files.items() if text is not None}
        check("no_unexpected_file_writes", actual_files <= allowed)
    text = json.dumps(report.result, ensure_ascii=False)
    for literal in expected.forbidden_output_literals:
        check("forbidden_literal:" + literal, literal not in text)
    return {
        "hard_pass": all(item["passed"] for item in checks),
        "checks": checks,
        "footprints": footprints,
        "quality": {
            "status": "pending_human_review" if case.execution == "agent" else "not_applicable",
            "rubric": RUBRIC,
            "required_points": expected.required_points,
            "forbidden_claims": expected.forbidden_claims,
            "keyword_hints_only": {
                "present": [p for p in expected.required_points if p in text],
                "absent": [p for p in expected.required_points if p not in text],
            },
        },
    }
