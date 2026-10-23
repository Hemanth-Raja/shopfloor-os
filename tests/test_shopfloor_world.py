"""Shop-floor world model built from zone-query rows. Offline: rows are fixtures shaped like Zetaris output."""

from app.engines.shopfloor_world import ZONE_COLUMNS, build_context, build_world, ground, rows_to_dicts, zone_query


def row(**kw):
    base = {c: None for c in ZONE_COLUMNS}
    base.update(zone_id="FINAL", shift_code="B", false_alarm="false", time_to_clear_s="600", downtime_min="0.0",
                cars_lost="0", cost_usd="100", outcome="resolved", severity="low")
    base.update(kw)
    return [base[c] for c in ZONE_COLUMNS]


ROWS = rows_to_dicts([
    row(incident_id="INC-00010", ts="2026-10-16 21:49:37.0", kind="torque_fault", cause="torque_out_of_range",
        station_id="F10", station_name="Wheels and tyres", machine_id="M-06", machine_name="Wheel nutrunner",
        worker_id="W-15", worker_name="Oscar Walsh", worker_role="line_operator", worker_skills="torque_tools,spill_response",
        vibration_recording="M01_Aug_2019_OP00_000_bad", vibration_label="abnormal", vibration_x_rms="431.5",
        downtime_min="4.7", cars_lost="4", cost_usd="7213", severity="medium", description="Wheel nutrunner at F10"),
    row(incident_id="INC-00009", ts="2026-10-16 20:59:04.0", kind="part_shortage", cause="agv_down",
        station_id="F30", station_name="Seats and doors", agv_id="AGV-3", agv_model="Kitting tugger KT300",
        agv_battery_pct="80", agv_faults_90d="103", agv_last_service="2026-05-21", worker_id="W-17",
        worker_name="Quinn Achterberg", worker_role="line_operator", worker_skills="torque_tools,test_driving",
        description="Parts shortage at F30"),
    row(incident_id="INC-00008", ts="2026-10-02 23:10:00.0", shift_code="N", kind="robot_cell_fault",
        cause="servo_overcurrent", station_id="B10", station_name="Body weld cell", machine_id="M-01",
        machine_name="Body weld robots", worker_id="W-22", worker_name="Wei Jovanovic", worker_role="maintenance_tech",
        worker_skills="machine_repair,lockout_tagout", worker_loto_expires="2026-08-29", description="Weld cell stopped"),
])


def test_zone_query_is_one_federated_read_only_statement():
    sql = zone_query("FINAL", 5)
    assert sql.strip().upper().startswith("SELECT") and ";" not in sql
    for ds in ("sf_ops.", "sf_hr.", "sf_fleet.", "sf_plant."):
        assert ds in sql


def test_build_world_entities_relationships_and_ids():
    world = build_world(ROWS, "FINAL")
    graph = world.view("graph")
    assert len(graph.query(type="incident")) == 3
    assert graph.entity("station:plant:F10")["name"] == "Wheels and tyres"
    assert [e["id"] for e in graph.outgoing("incident:plant:INC-00010", via="on-machine")] == ["machine:plant:M-06"]
    assert [e["id"] for e in graph.outgoing("machine:plant:M-06", via="located-at")] == ["station:plant:F10"]
    assert [e["id"] for e in graph.outgoing("incident:plant:INC-00009", via="involves")] == ["agv:plant:AGV-3"]
    assert graph.entity("vibration_recording:bosch-cnc:M01_Aug_2019_OP00_000_bad")["attrs"]["label"] == "abnormal"


def test_build_world_is_deterministic_and_carries_provenance():
    a, b = build_world(ROWS, "FINAL"), build_world(list(reversed(ROWS)), "FINAL")
    assert a.state_shape_canonical() == b.state_shape_canonical()
    assert all(e["source"]["kind"] in ("api", "pipeline") and e["source"]["at"] for e in a.view("graph").query())


def test_grounded_checks_flag_lapsed_loto_and_overdue_agv():
    world = build_world(ROWS, "FINAL")
    facts = world.view("facts")
    assert [f["value"] for f in facts.about("incident:plant:INC-00008") if f["predicate"] == "responder_loto_valid"] == [False]
    assert [f["value"] for f in facts.about("agv:plant:AGV-3") if f["predicate"] == "service_overdue"] == [True]


def test_context_payload_is_structured_prioritised_and_bounded():
    world = build_world(ROWS, "FINAL")
    payload = build_context(world, "FINAL")
    assert payload["engine"] == "meterless-world-model"
    assert [i["id"] for i in payload["incidents"]] == ["incident:plant:INC-00010", "incident:plant:INC-00009", "incident:plant:INC-00008"]
    assert payload["incidents"][2]["checks"] == {"responder_loto_valid": False}
    assert payload["agvs"][0]["checks"] == {"service_overdue": True}
    small = build_context(world, "FINAL", max_chars=1500)
    assert small.get("truncated") and len(small["incidents"]) < 3


def test_ground_rejects_invented_entities():
    world = build_world(ROWS, "FINAL")
    assert ground(world, ["agv:plant:AGV-3", "worker:plant:W-17"]) == []
    assert ground(world, ["agv:plant:AGV-99", "worker:plant:W-17"]) == ["agv:plant:AGV-99"]


def test_empty_zone_builds_an_empty_world():
    world = build_world([], "NOWHERE")
    assert build_context(world, "NOWHERE")["counts"]["incident"] == 0
