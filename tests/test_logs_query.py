"""The `stocks logs` read side (stocks.logs_query): filters, rendering, stats."""

from __future__ import annotations

import json

import pytest

from stocks import logs_query as lq


def entry(**payload) -> dict:
    sev = payload.pop("severity", "INFO")
    return {
        "timestamp": payload.pop("timestamp", "2026-08-31T13:29:11.000Z"),
        "severity": sev,
        "jsonPayload": payload,
    }


# ------------------------------------------------------------------- filters


def test_filter_excludes_the_access_log_by_default():
    f = lq.build_filter(project="p", service="svc")
    assert 'resource.labels.service_name="svc"' in f
    assert 'logName!="projects/p/logs/run.googleapis.com%2Frequests"' in f


def test_admin_audit_entries_are_excluded_from_the_app_view():
    # A 403 on a console API call is logged against the same resource at ERROR;
    # it is not the app failing.
    assert 'NOT logName:"cloudaudit"' in lq.build_filter()
    assert "cloudaudit" not in lq.build_filter(http=True)


def test_http_flag_selects_the_access_log_instead():
    f = lq.build_filter(project="p", http=True)
    assert 'logName="projects/p/logs/run.googleapis.com%2Frequests"' in f
    assert "logName!=" not in f


def test_structured_options_become_field_equalities():
    f = lq.build_filter(level="error", event="page.render", user="u_1",
                        revision="topstocks-00005-r7w")
    assert "severity>=ERROR" in f
    assert 'jsonPayload.event="page.render"' in f
    assert 'jsonPayload.user="u_1"' in f
    assert 'resource.labels.revision_name="topstocks-00005-r7w"' in f


def test_grep_searches_both_payload_shapes():
    f = lq.build_filter(grep="rate limit")
    assert 'textPayload:"rate limit"' in f
    assert 'jsonPayload.message:"rate limit"' in f


def test_grep_quotes_are_escaped():
    assert '\\"boom\\"' in lq.build_filter(grep='say "boom"')


# ------------------------------------------------------------------ payloads


def test_payload_reads_free_text_lines():
    assert lq.payload({"textPayload": "HTTP Error 404"})["message"] == "HTTP Error 404"


def test_payload_summarizes_access_log_entries():
    body = lq.payload({"httpRequest": {"requestMethod": "GET", "status": 500,
                                       "requestUrl": "/portfolio",
                                       "latency": "1.2s", "remoteIp": "1.2.3.4"}})
    assert body["message"] == "GET /portfolio"
    assert body["status"] == 500


# ----------------------------------------------------------------- rendering


def test_render_is_oldest_first_with_the_fields_appended():
    out = lq.render([
        entry(event="b", message="second", backend="groq"),
        entry(event="a", message="first"),
    ])
    lines = out.splitlines()
    assert lines[0].split()[3] == "a"       # the older entry leads
    assert "backend=groq" in lines[1]


def test_render_hides_stack_traces_unless_asked():
    e = entry(event="page.render", message="boom", severity="ERROR",
              stack_trace="Traceback...\n  ValueError")
    assert "Traceback" not in lq.render([e])
    assert "Traceback" in lq.render([e], show_trace=True)


def test_multiline_messages_collapse_to_one_row():
    out = lq.render([entry(event="x", message="head\nTraceback (most recent...)")])
    assert out.count("\n") == 0
    assert "Traceback" not in out


# --------------------------------------------------------------------- stats


def test_stats_counts_errors_and_percentiles_per_event():
    rows = lq.stats([
        entry(event="page.render", duration_ms=100),
        entry(event="page.render", duration_ms=300),
        entry(event="page.render", severity="ERROR", duration_ms=900),
        entry(event="chat.answered", duration_ms=50),
    ])
    page = next(r for r in rows if r["key"] == "page.render")
    assert page["count"] == 3
    assert page["errors"] == 1
    assert page["max_ms"] == 900
    assert page["p50_ms"] == 300
    assert rows[0]["key"] == "page.render"  # busiest first


def test_stats_groups_by_any_field():
    rows = lq.stats([entry(event="e", user="u_1"), entry(event="e", user="u_2")],
                    by="user")
    assert {r["key"] for r in rows} == {"u_1", "u_2"}


def test_stats_keeps_unstructured_lines_visible():
    rows = lq.stats([{"timestamp": "", "severity": "ERROR",
                      "textPayload": "HTTP Error 404"}])
    assert rows[0]["key"] == "-"
    assert rows[0]["errors"] == 1
    assert rows[0]["p50_ms"] is None


def test_render_stats_handles_an_empty_range():
    assert "no entries" in lq.render_stats([])


# ------------------------------------------------------------- export/import


def test_export_round_trips_through_jsonl(tmp_path):
    entries = [entry(event="a"), entry(event="b")]
    out = lq.export(entries, tmp_path / "snap.jsonl")
    assert lq.read_file(out) == entries


def test_read_surfaces_gcloud_failures_as_logs_error(monkeypatch):
    monkeypatch.setattr(lq.shutil, "which", lambda _: "/usr/bin/gcloud")

    class Proc:
        returncode = 1
        stdout = ""
        stderr = "ERROR: (gcloud.logging.read) You do not have permission"

    monkeypatch.setattr(lq.subprocess, "run", lambda *a, **k: Proc())
    with pytest.raises(lq.LogsError, match="permission"):
        lq.read("filter")


def test_read_without_gcloud_explains_how_to_get_it(monkeypatch):
    monkeypatch.setattr(lq.shutil, "which", lambda _: None)
    with pytest.raises(lq.LogsError, match="gcloud auth login"):
        lq.read("filter")


def test_read_parses_the_gcloud_json_output(monkeypatch):
    monkeypatch.setattr(lq.shutil, "which", lambda _: "/usr/bin/gcloud")
    captured = {}

    class Proc:
        returncode = 0
        stdout = json.dumps([entry(event="a")])
        stderr = ""

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return Proc()

    monkeypatch.setattr(lq.subprocess, "run", fake_run)
    got = lq.read("my-filter", project="p", freshness="6h", limit=5)
    assert got[0]["jsonPayload"]["event"] == "a"
    assert "my-filter" in captured["cmd"]
    assert "--freshness" in captured["cmd"] and "6h" in captured["cmd"]


# --------------------------------------------------------------------- usage


def test_usage_rolls_up_users_pages_and_turns_per_day():
    entries = [
        entry(event="page.render", page="Home", user="jane_1a"),
        entry(event="page.render", page="Cartera", user="jane_1a"),
        entry(event="page.render", page="Home", user="bob_2b"),
        entry(event="chat.request", user="jane_1a"),
        entry(event="feedback", user="bob_2b"),
        entry(
            event="page.render", page="Home", user="jane_1a",
            timestamp="2026-09-01T09:00:00.000Z",
        ),
        entry(message="free text, no event"),  # never crashes the rollup
    ]
    s = lq.usage(entries)
    assert s["total_users"] == 2
    d1, d2 = s["days"]
    assert (d1["day"], d1["users"], d1["runs"], d1["chat"], d1["feedback"]) == (
        "2026-08-31", 2, 3, 1, 1,
    )
    assert (d2["day"], d2["users"], d2["runs"]) == ("2026-09-01", 1, 1)
    assert s["pages"][0] == ("Home", 3)


def test_usage_renders_and_survives_empty_input():
    assert lq.render_usage(lq.usage([])) == "(no entries in range)"
    text = lq.render_usage(lq.usage([entry(event="page.render", page="Home",
                                           user="jane_1a")]))
    assert "2026-08-31" in text and "unique users in range: 1" in text
