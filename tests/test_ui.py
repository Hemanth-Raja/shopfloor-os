"""The page explains itself, and the health panel has an offline status (no 'health unavailable')."""

from pathlib import Path

HTML = (Path(__file__).resolve().parents[1] / "web" / "index.html").read_text()


def test_page_has_first_time_guidance():
    assert 'id="howto"' in HTML and "How to read this page" in HTML and 'id="howto-close"' in HTML
    assert 'class="act start"' in HTML and 'content:"Start here"' in HTML
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
