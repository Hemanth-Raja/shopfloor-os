"""The page explains itself, and the health panel has an offline status (no 'health unavailable')."""

from pathlib import Path

HTML = (Path(__file__).resolve().parents[1] / "web" / "index.html").read_text()


def test_page_has_first_time_guidance():
    assert 'id="howto"' in HTML and "How to read this page" in HTML and 'id="howto-close"' in HTML
    assert 'class="act start"' in HTML and 'Start here"' in HTML
    assert 'id="narration"' in HTML and "function narrate(" in HTML
    for area in ("Body shop", "Paint shop", "Trim line", "Final assembly", "End-of-line test", "Parts supermarket", "AGV chargers"):
        assert area in HTML


def test_narration_covers_every_agent_step():
    for step in ("reflex", "monitor", "memory", "candidates", "planner", "critic", "dryrun", "restraint", "executor",
                 "verifier", "safe_mode", "human"):
        assert f'case "{step}"' in HTML.split("function narrate(")[1].split("async function load()")[0]


def test_offline_health_reports_each_dependency(monkeypatch):
    monkeypatch.setenv("SAMPLE_MODE", "true")
    from app import server
    h = server.App(offline=True, replay=True).health()
    assert h["mode"] == "offline" and h["zetaris"]["status"] == "offline" and h["nightshift"]["status"] == "offline"
    assert h["floor"]["status"] == "ok" and h["overall"] == "ok"


def test_flow_tab_has_every_agent_and_both_loops():
    assert 'data-view="flow"' in HTML and 'id="flownodes"' in HTML and 'id="flowdetail"' in HTML
    flow = HTML.split("const FLOW = [")[1].split("];")[0]
    for step in ("reflex", "monitor", "memory", "candidates", "planner", "critic", "dryrun", "restraint", "gate",
                 "executor", "verifier"):
        assert f'"{step}"' in flow
    assert 'id="loop-critic"' in HTML and 'id="loop-replan"' in HTML


def test_watch_describes_steps_in_plain_english():
    from app.watch import describe_step, model_name
    assert model_name("nvidia/nemotron-3.5-super-vl-120b-a12b") == "Nemotron-Super"
    line = describe_step({"step": "critic", "ok": False, "detail": {"violations": [
        {"rule_id": "R-CROSSING", "detail": "AGV-5 too fast", "cfr_ref": "x", "citations": [{"ref": "29 CFR 1910.178(n)(4)"}]}]}})
    assert line.startswith("REJECTED") and "29 CFR 1910.178(n)(4)" in line
