"""The tables the investigator may query through Zetaris (generated from the data generator's schemas)."""

CATALOG: dict[str, list[str]] = {
    'sf_fleet.agvs': ['agv_id', 'model', 'kind', 'home_x', 'home_y', 'battery_pct', 'battery_health_pct', 'status', 'max_payload_kg', 'top_speed_mps', 'commissioned_date', 'last_service_date', 'faults_90d'],
    'sf_fleet.agv_tasks': ['task_id', 'agv_id', 'task_type', 'shift_code', 'from_zone', 'to_location', 'start_ts', 'end_ts', 'distance_m', 'payload_kg', 'status', 'delay_s'],
    'sf_fleet.agv_telemetry': ['ts', 'agv_id', 'x', 'y', 'speed_mps', 'battery_pct', 'state', 'fault_code', 'task_id'],
    'sf_fleet.fleet_hourly': ['ts', 'shift_code', 'producing', 'kitting_demand_agvs', 'kitting_agvs_available', 'utilisation_pct', 'outdoor_temp_c'],
    'sf_hr.workers': ['worker_id', 'name', 'role', 'shift_code', 'station_id', 'home_zone', 'skills', 'forklift_licence', 'x', 'y', 'hire_date'],
    'sf_hr.certifications': ['worker_id', 'cert', 'issued_date', 'expires_date'],
    'sf_hr.shifts': ['worker_id', 'shift_date', 'shift_code', 'start_time', 'end_time', 'status'],
    'sf_plant.stations': ['station_id', 'name', 'zone_id', 'x', 'y', 'kitted', 'buffer_min'],
    'sf_plant.machines': ['machine_id', 'name', 'kind', 'station_id', 'zone_id', 'x', 'y', 'commissioned_year'],
    'sf_plant.machine_telemetry': ['ts', 'machine_id', 'air_temp_k', 'process_temp_k', 'rpm', 'torque_nm', 'tool_wear_min', 'failure', 'heat_dissipation_failure', 'overstrain_failure', 'source'],
    'sf_plant.vibration_samples': ['recording', 'bosch_machine', 'operation', 'period', 'label', 'duration_s', 'x_rms', 'x_peak', 'x_crest', 'x_kurtosis', 'y_rms', 'y_peak', 'y_crest', 'y_kurtosis', 'z_rms', 'z_peak', 'z_crest', 'z_kurtosis', 'linked_machine_id'],
    'sf_plant.weather_hourly': ['ts', 'temperature_c', 'source'],
    'sf_plant.production_plan': ['shift_date', 'shift_code', 'planned_units', 'built_units', 'downtime_min', 'incidents', 'andon_pulls', 'overtime', 'takt_s'],
    'sf_plant.units': ['unit_id', 'model', 'body_style', 'variant', 'colour', 'shift_date', 'shift_code', 'completed_ts', 'status', 'defect_code'],
    'sf_plant.work_orders': ['wo_id', 'created_ts', 'asset_type', 'asset_id', 'wo_type', 'priority', 'assigned_to', 'status', 'closed_ts', 'labour_min', 'incident_id', 'notes'],
    'sf_ops.zones': ['zone_id', 'name', 'kind', 'x0', 'y0', 'x1', 'y1'],
    'sf_ops.floor_cells': ['x', 'y', 'cell'],
    'sf_ops.spill_kits': ['kit_id', 'x', 'y', 'zone_id'],
    'sf_ops.safety_rules': ['rule_id', 'cfr_ref', 'summary', 'check_type', 'params'],
    'sf_ops.incidents': ['incident_id', 'ts', 'shift_code', 'kind', 'cause', 'zone_id', 'station_id', 'machine_id', 'agv_id', 'x', 'y', 'severity', 'reported_by', 'worker_id', 'response_s', 'time_to_clear_s', 'downtime_min', 'cars_lost', 'cost_usd', 'outcome', 'false_alarm', 'supermarket_distance', 'spill_kit_distance', 'outdoor_temp_c', 'agv_utilisation_pct', 'osha_group', 'recordable_injury', 'vibration_recording', 'description'],
    'sf_ops.andon_events': ['andon_id', 'ts', 'shift_code', 'station_id', 'reason', 'responder_id', 'response_s', 'resolved_s', 'stopped_line', 'incident_id'],
    'sf_ops.data_sources': ['source', 'used_for', 'status', 'licence', 'credit'],
    'sf_ops.experiments': ['experiment_id', 'created_at', 'hypothesis', 'policy', 'baseline_mean_s', 'treatment_mean_s', 'effect_pct', 'ci_low', 'ci_high', 'status'],
}

NOTES = """Values worth knowing:
- sf_ops.incidents.kind: part_shortage, robot_cell_fault, torque_fault, machine_overheat, agv_fault, fluid_spill,
  near_miss, quality_defect. shift_code: A (06-14), B (14-22), N (22-06). ts is a TIMESTAMP (Jul-Oct 2026).
  station_id: B10 B20 P10 T10 T20 M10 F10 F20 F30 E10; agv_id: AGV-1..AGV-8; machine_id: M-01..M-08.
  false_alarm is BOOLEAN; downtime_min, cars_lost, cost_usd are numbers; vibration_recording joins
  sf_plant.vibration_samples.recording (label normal/abnormal).
- sf_fleet.agvs.faults_90d, battery_health_pct, last_service_date; sf_fleet.agv_tasks.status: done/late/aborted.
- sf_plant.production_plan: planned_units vs built_units per shift_date and shift_code.
- sf_plant.machine_telemetry: hourly AI4I readings per machine_id (failure flags 0/1).
- sf_plant.weather_hourly.temperature_c is the outdoor temperature (Detroit) per hour, ts on the hour. Incidents
  already carry outdoor_temp_c; to join weather use date_trunc('HOUR', i.ts) = w.ts, never i.ts = w.ts.
SQL dialect: Zetaris Lightning (Spark SQL). Qualify every table as sf_<db>.<table>. One SELECT, no semicolon."""


# The agents' own history, copied from logs/ by app/logsync.py and app/journal.py. Shown to the models only when a
# question is about the agents (keeps plant questions' prompts short and their recorded replies valid).
AGENT_CATALOG: dict[str, list[str]] = {
    'sf_ops.agent_steps': ['id', 'ts', 'run_id', 'incident', 'agent', 'ok', 'ms', 'attempt', 'model', 'decision', 'rules',
                           'detail'],
    'sf_ops.llm_calls': ['id', 'ts', 'role', 'requested_model', 'answering_model', 'latency_s', 'prompt_tokens',
                         'completion_tokens', 'truncated'],
    'sf_ops.journal': ['seq', 'ts', 'kind', 'data', 'prev', 'hash'],
}
AGENT_NOTES = """Agent history (our own logs, synced into the data layer):
- sf_ops.agent_steps: one row per agent step. agent: reflex, monitor, memory, candidates, planner, critic, dryrun,
  refine, restraint, gate, executor, verifier, router, safe_mode. ok is BOOLEAN (critic ok=false is a rejection);
  rules is a comma list of violated rule ids (R-CROSSING, R-LOTO, R-TORQUE-QA...); decision is ACT, VERIFY,
  SAFE_HOLD, ESCALATE or DO_NOTHING on restraint steps; model is the answering model.
- sf_ops.llm_calls: one row per Nemotron call: role, requested_model vs answering_model, latency_s, tokens.
- sf_ops.journal: the shift journal; kind is queued, started, finished, lease, release, preempt, approval,
  shift_started, shift_finished; data is JSON text."""
AGENT_WORDS = ("agent", "critic", "planner", "nemotron", "model", "router", "orchestrator", "reject", "rule",
               "decision", "approv", "escalat", "roll", "journal", "lease", "pre-empt", "preempt", "latency", "token",
               "verifier", "dry run", "dryrun", "restraint", "lesson")


def about_agents(question: str) -> bool:
    q = (question or "").lower()
    return any(w in q for w in AGENT_WORDS)
