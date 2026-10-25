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


def test_flow_lives_in_incidents_for_a_named_incident_with_every_agent_and_a_models_panel():
    assert 'data-view="flow"' not in HTML  # no separate Flow tab: it showed a flow without saying for which incident
    incs = HTML.split('<section class="view" id="incs">')[1].split("</section>")[0]
    assert 'id="flowgraph"' in incs and 'id="flowdetail"' in incs and 'id="hops"' in incs and 'id="flowhead"' in incs
    assert '"Agent flow: " + flowMeta' in HTML and "steps.forEach(flowStep)" in HTML  # titled with, and fed by, the incident
    assert 'id="models"' in HTML.split('<section class="view" id="nemo">')[1].split("</section>")[0]
    nodes = HTML.split("const NODES = {")[1].split("};")[0]
    for step in ("reflex", "monitor", "memory", "investigator", "candidates", "playbook", "verify_plan", "planner",
                 "critic", "dryrun", "restraint", "gate", "executor", "verifier", "safe_mode"):
        assert f"{step}:" in nodes
    assert 'id="cmp"' in HTML and 'id="models"' in HTML and "/api/models" in HTML


def test_watch_describes_steps_in_plain_english():
    from app.watch import describe_step, model_name
    assert model_name("nvidia/nemotron-3.5-super-vl-120b-a12b") == "Nemotron-Super"
    line = describe_step({"step": "critic", "ok": False, "detail": {"violations": [
        {"rule_id": "R-CROSSING", "detail": "AGV-5 too fast", "cfr_ref": "x", "citations": [{"ref": "29 CFR 1910.178(n)(4)"}]}]}})
    assert line.startswith("REJECTED") and "29 CFR 1910.178(n)(4)" in line


def test_incidents_tab_lists_incidents_and_shows_one_history():
    assert 'data-view="incs"' in HTML and 'id="incrows"' in HTML and 'id="incdetail"' in HTML
    assert "/api/incidents" in HTML and '"/api/incident/"' in HTML and "async function incShow(" in HTML
    body = HTML.split("async function incShow(")[1].split("es.addEventListener")[0]
    assert 'innerHTML = "loading…"' in body.split("await fetch")[0]  # the click shows at once, before the fetch
    assert "human_actions" in body and "openIncident(id)" in body


def test_flow_compare_is_hidden_until_a_previous_incident_exists():
    assert 'id="cmplabel" style="display:none"' in HTML
    assert '$("cmplabel").style.display = flowPrev ?' in HTML


def test_run_buttons_lock_per_click_and_open_incidents_are_listed():
    body = HTML.split("async function startRun(")[1].split("\n}\n")[0]
    assert body.index("btn.disabled = true") < body.index("await fetch")  # locked before the request goes out
    assert 'id="floorlist"' in HTML and '"/api/floor"' in HTML
    assert "if (st.incident === current) addStep(st)" in HTML  # each incident's steps stay in its own trace


def test_incidents_say_where_they_came_from_and_can_be_filtered():
    assert 'id="incfilter"' in HTML and "srcBadge(r.source)" in HTML and 'class="src live"' in HTML


def test_ask_keeps_a_history_and_compares_a_repeated_question():
    assert 'id="askhist"' in HTML and '"/api/asks"' in HTML and '"/api/ask/"' in HTML
    assert "Asked before at" in HTML and "ch.new_findings" in HTML and "ch.gone_findings" in HTML


def test_incidents_filter_never_shows_a_hidden_incidents_flow_and_keeps_the_selection_in_view():
    body = HTML.split("async function incList(")[1].split("// compact key/value")[0]
    assert "incClear(" in body and "filterChanged && !listed" in body and "scrollTop = keep" in body
    assert "incScroll();" in HTML.split("async function incShow(")[1].split("await fetch")[0]
