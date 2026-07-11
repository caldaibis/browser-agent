import json
from pathlib import Path

from src.browser_token_metrics import build_report


def _write_session(path: Path, *, backend: str | None, totals: list[int], outcome: str):
    start = {"model": "deepseek-v4-pro"}
    if backend:
        start.update({"browser_backend": backend, "browser_backend_version": "1.0"})
    events = [{"event": "run_start", "payload": start}]
    events.extend({
        "event": "turn_usage",
        "payload": {
            "prompt_tokens": total - 10,
            "completion_tokens": 10,
            "total_tokens": total,
            "reasoning_tokens": 2,
            "cache_hit_tokens": 3,
        },
    } for total in totals)
    events.append({"event": "final", "payload": {"outcome": outcome}})
    path.write_text("".join(json.dumps(event) + "\n" for event in events))


def test_token_metrics_group_sum_and_describe_sessions(tmp_path):
    _write_session(tmp_path / "old.jsonl", backend=None, totals=[100, 200], outcome="submitted")
    _write_session(
        tmp_path / "new-1.jsonl", backend="agent_browser", totals=[50], outcome="submitted")
    _write_session(
        tmp_path / "new-2.jsonl", backend="agent_browser", totals=[70, 80], outcome="incomplete")

    report = build_report(tmp_path, minimum_sample_size=2)
    assert report["sessions"] == 3
    assert report["comparison_ready"] is False

    legacy = report["cohorts"]["playwright@legacy-unrecorded|deepseek-v4-pro"]
    assert legacy["total_tokens_per_session"]["mean"] == 300
    assert legacy["turns_per_session"]["mean"] == 2
    assert legacy["minimum_sample_reached"] is False

    current = report["cohorts"]["agent_browser@1.0|deepseek-v4-pro"]
    assert current["total_tokens_per_session"] == {
        "mean": 100.0, "median": 100.0, "p90": 150, "min": 50, "max": 150,
    }
    assert current["prompt_tokens_per_session"]["mean"] == 85
    assert current["outcomes"] == {"incomplete": 1, "submitted": 1}
    assert current["token_observed_sessions"] == 2
    assert current["token_missing_sessions"] == 0
    assert current["total_tokens_per_submitted_session"]["mean"] == 50


def test_token_metrics_skip_invalid_and_empty_trajectories(tmp_path):
    (tmp_path / "broken.jsonl").write_text("not json\n")
    (tmp_path / "empty.jsonl").write_text(json.dumps({
        "event": "run_start", "payload": {"browser_backend": "agent_browser"},
    }) + "\n")
    assert build_report(tmp_path)["sessions"] == 0


def test_zero_provider_usage_is_counted_but_excluded_from_distributions(tmp_path):
    _write_session(tmp_path / "zero.jsonl", backend=None, totals=[0], outcome="submitted")
    _write_session(tmp_path / "valid.jsonl", backend=None, totals=[200], outcome="submitted")
    cohort = build_report(tmp_path)["cohorts"][
        "playwright@legacy-unrecorded|deepseek-v4-pro"
    ]
    assert cohort["sessions"] == 2
    assert cohort["token_observed_sessions"] == 1
    assert cohort["token_missing_sessions"] == 1
    assert cohort["total_tokens_per_session"]["mean"] == 200
    assert cohort["total_tokens_per_submitted_session"]["mean"] == 200
