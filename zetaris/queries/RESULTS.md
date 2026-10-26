# Zetaris query results

Run on 26 Oct 2026 22:27 CET by `python scripts/zetaris_queries.py` through `app/zetaris.py` `run_sql` (login: read-only role). Real rows from the local Zetaris instance; at most 12 rows shown per query.

## dq

Zetaris data quality on the USL: the rule `cert_in_date`, registered by `python -m app.usl dq` (REGISTER DQ is an admin write; these two reads are not).

Source: [`dq.sql`](dq.sql)

### Which key constraints and data-quality rules does Zetaris hold for the USL?

```sql
LIST DQ USL lightning.metastore.shopfloor.floor
```

23 rows, 53 ms.

| col1 | col2 | col3 | col4 |
|---|---|---|---|
| zone_id | zone | Primary key constraints |  |
| station_id | station | Primary key constraints |  |
| zone_id | station | Foreign key constraints |  |
| machine_id | machine | Primary key constraints |  |
| station_id | machine | Foreign key constraints |  |
| zone_id | machine | Foreign key constraints |  |
| worker_id | worker | Primary key constraints |  |
| station_id | worker | Foreign key constraints |  |
| home_zone | worker | Foreign key constraints |  |
| cert_id | certification | Primary key constraints |  |
| worker_id | certification | Foreign key constraints |  |
| cert_in_date | certification | Custom Data Quality | expires_date IS NOT NULL AND expires_date >= DATE '2026-10-16' |

_11 more rows not shown._

### Which certificates fail the rule `cert_in_date` (not in date on 16 Oct 2026)?

```sql
SHOW DQ INVALID RECORD cert_in_date TABLE lightning.metastore.shopfloor.floor.certification
```

2 rows, 227 ms.

| col1 | col2 | col3 | col4 | col5 |
|---|---|---|---|---|
| W-21:forklift | W-21 | forklift | 2023-08-15 | 2026-08-14 |
| W-22:lockout_tagout | W-22 | lockout_tagout | 2025-08-29 | 2026-08-29 |

## sf_fleet

sf_fleet: the AGV fleet (Postgres database `fleet`). The candidates agent uses it to send only healthy robots.

Source: [`sf_fleet.sql`](sf_fleet.sql)

### Is the fleet operable, and how healthy is each status group?

```sql
SELECT status, count(*) AS agvs, round(avg(battery_health_pct), 1) AS avg_battery_health_pct, sum(faults_90d) AS faults_90d
FROM sf_fleet.agvs GROUP BY status ORDER BY agvs DESC
```

1 rows, 213 ms.

| status | agvs | avg_battery_health_pct | faults_90d |
|---|---|---|---|
| idle | 8 | 86.6 | 200 |

### Which AGVs abort or delay the most tasks?

```sql
SELECT agv_id, count(*) AS tasks, sum(CASE WHEN status = 'aborted' THEN 1 ELSE 0 END) AS aborted,
       sum(CASE WHEN status = 'late' THEN 1 ELSE 0 END) AS late, round(avg(delay_s), 1) AS avg_delay_s
FROM sf_fleet.agv_tasks GROUP BY agv_id ORDER BY aborted DESC LIMIT 8
```

8 rows, 240 ms.

| agv_id | tasks | aborted | late | avg_delay_s |
|---|---|---|---|---|
| AGV-3 | 13227 | 158 | 611 | 11.8 |
| AGV-2 | 14214 | 43 | 662 | 11.4 |
| AGV-4 | 14339 | 36 | 647 | 11.0 |
| AGV-5 | 14334 | 35 | 674 | 11.1 |
| AGV-1 | 14365 | 35 | 627 | 10.3 |
| AGV-6 | 8462 | 17 | 320 | 9.5 |
| AGV-8 | 8528 | 7 | 317 | 8.7 |
| AGV-7 | 8495 | 7 | 336 | 9.6 |

### Which fault codes does AGV telemetry report most, and on how many robots?

```sql
SELECT fault_code, count(*) AS readings, count(DISTINCT agv_id) AS agvs
FROM sf_fleet.agv_telemetry WHERE fault_code IS NOT NULL AND fault_code <> ''
GROUP BY fault_code ORDER BY readings DESC LIMIT 8
```

5 rows, 132 ms.

| fault_code | readings | agvs |
|---|---|---|
| E52_NAV_LOST | 353 | 7 |
| E40_BATTERY_LOW | 53 | 2 |
| E31_LIDAR_BLOCKED | 28 | 2 |
| E12_WHEEL_SLIP | 13 | 2 |
| BLOCKED_LANE | 7 | 1 |

## sf_hr

sf_hr: people, certificates and rosters (Postgres database `hr`). The critic and the gate block anyone without an in-date certificate.

Source: [`sf_hr.sql`](sf_hr.sql)

### Who works here, by role and shift?

```sql
SELECT role, shift_code, count(*) AS workers FROM sf_hr.workers GROUP BY role, shift_code ORDER BY role, shift_code
```

14 rows, 156 ms.

| role | shift_code | workers |
|---|---|---|
| line_operator | A | 6 |
| line_operator | B | 6 |
| maintenance_tech | A | 1 |
| maintenance_tech | B | 1 |
| maintenance_tech | N | 1 |
| material_handler | A | 1 |
| material_handler | B | 1 |
| material_handler | N | 1 |
| quality_inspector | A | 1 |
| quality_inspector | B | 1 |
| shift_supervisor | A | 1 |
| shift_supervisor | N | 1 |

_2 more rows not shown._

### Which certificates have lapsed on the plant date (16 Oct 2026)?

```sql
SELECT cert, count(*) AS held, sum(CASE WHEN expires_date < DATE '2026-10-16' THEN 1 ELSE 0 END) AS lapsed,
       min(expires_date) AS earliest_expiry
FROM sf_hr.certifications GROUP BY cert ORDER BY lapsed DESC, cert
```

4 rows, 190 ms.

| cert | held | lapsed | earliest_expiry |
|---|---|---|---|
| forklift | 3 | 1 | 2026-08-14 |
| lockout_tagout | 4 | 1 | 2026-08-29 |
| first_aid | 6 | 0 | 2027-05-24 |
| robot_cell_entry | 5 | 0 | 2027-06-15 |

### Who is rostered on the plant date, by shift?

```sql
SELECT shift_code, status, count(*) AS workers FROM sf_hr.shifts
WHERE shift_date = DATE '2026-10-16' GROUP BY shift_code, status ORDER BY shift_code, status
```

7 rows, 122 ms.

| shift_code | status | workers |
|---|---|---|
| A | leave | 1 |
| A | on | 10 |
| B | absent | 1 |
| B | leave | 1 |
| B | on | 8 |
| N | absent | 2 |
| N | on | 1 |

## sf_ops

sf_ops: incidents, andon pulls, safety rules, the floor grid, and the agents' own logs (Postgres database `ops`).

Source: [`sf_ops.sql`](sf_ops.sql)

### Which incident kinds cost the most, and how many are false alarms?

```sql
SELECT kind, count(*) AS incidents, round(100.0 * sum(CASE WHEN false_alarm THEN 1 ELSE 0 END) / count(*), 1) AS false_alarm_pct,
       round(sum(downtime_min), 0) AS downtime_min, sum(cars_lost) AS cars_lost
FROM sf_ops.incidents GROUP BY kind ORDER BY downtime_min DESC
```

8 rows, 251 ms.

| kind | incidents | false_alarm_pct | downtime_min | cars_lost |
|---|---|---|---|---|
| part_shortage | 821 | 0.0 | 2919.0 | 2333 |
| robot_cell_fault | 274 | 3.3 | 2285.0 | 1826 |
| torque_fault | 225 | 10.7 | 1413.0 | 1127 |
| machine_overheat | 100 | 26.0 | 742.0 | 592 |
| fluid_spill | 82 | 0.0 | 150.0 | 120 |
| agv_fault | 213 | 6.1 | 106.0 | 86 |
| near_miss | 88 | 0.0 | 70.0 | 56 |
| quality_defect | 268 | 0.0 | 0.0 | 0 |

### Which andon reasons stop the line, and how fast does someone respond?

```sql
SELECT reason, count(*) AS pulls, sum(CASE WHEN stopped_line THEN 1 ELSE 0 END) AS stopped_line,
       round(avg(response_s), 0) AS avg_response_s
FROM sf_ops.andon_events GROUP BY reason ORDER BY pulls DESC
```

6 rows, 176 ms.

| reason | pulls | stopped_line | avg_response_s |
|---|---|---|---|
| missing_part | 1006 | 314 | 109.0 |
| equipment | 585 | 473 | 85.0 |
| quality_check | 549 | 8 | 78.0 |
| help_needed | 360 | 15 | 55.0 |
| tool_issue | 171 | 6 | 55.0 |
| safety | 90 | 36 | 55.0 |

### Which rules does the safety critic reject plans for most often (the agents' own steps, synced from logs/)?

```sql
SELECT rules, count(*) AS rejections FROM sf_ops.agent_steps
WHERE agent = 'critic' AND ok = false AND rules IS NOT NULL AND rules <> ''
GROUP BY rules ORDER BY rejections DESC LIMIT 8
```

8 rows, 152 ms.

| rules | rejections |
|---|---|
| R-CROSSING | 604 |
| R-TORQUE-QA | 552 |
| R-LOTO | 241 |
| R-CONTAIN | 123 |
| R-TRAIN | 36 |
| R-HAZARD-ZONE | 29 |
| R-LEASE | 8 |
| R-NEAR-MISS | 3 |

## sf_plant

sf_plant: stations, machines, telemetry, production (Postgres database `plant`). The monitor reads telemetry and Bosch vibration; the World Model reads the layout.

Source: [`sf_plant.sql`](sf_plant.sql)

### How is the line laid out: stations per zone, kitting and the thinnest line-side buffer?

```sql
SELECT zone_id, count(*) AS stations, sum(CASE WHEN kitted THEN 1 ELSE 0 END) AS kitted, min(buffer_min) AS min_buffer_min
FROM sf_plant.stations GROUP BY zone_id ORDER BY zone_id
```

6 rows, 177 ms.

| zone_id | stations | kitted | min_buffer_min |
|---|---|---|---|
| BODY | 2 | 0 | 20 |
| EOL | 1 | 0 | 0 |
| FINAL | 3 | 2 | 0 |
| MARR | 1 | 1 | 10 |
| PAINT | 1 | 0 | 20 |
| TRIM | 2 | 2 | 12 |

### Which machines fail most in their telemetry (AI4I-shaped readings)?

```sql
SELECT machine_id, count(*) AS readings, sum(failure) AS failures, sum(heat_dissipation_failure) AS heat_failures,
       sum(overstrain_failure) AS overstrain_failures
FROM sf_plant.machine_telemetry GROUP BY machine_id ORDER BY failures DESC LIMIT 8
```

8 rows, 138 ms.

| machine_id | readings | failures | heat_failures | overstrain_failures |
|---|---|---|---|---|
| M-04 | 2160 | 164 | 114 | 24 |
| M-03 | 2160 | 136 | 90 | 21 |
| M-01 | 2160 | 55 | 0 | 23 |
| M-02 | 2160 | 49 | 1 | 20 |
| M-06 | 2160 | 47 | 0 | 16 |
| M-07 | 2160 | 47 | 0 | 22 |
| M-05 | 2160 | 46 | 0 | 18 |
| M-08 | 2160 | 46 | 0 | 25 |

### How close does each shift get to the production plan?

```sql
SELECT shift_code, sum(planned_units) AS planned, sum(built_units) AS built,
       round(100.0 * sum(built_units) / sum(planned_units), 1) AS attainment_pct, round(sum(downtime_min), 0) AS downtime_min
FROM sf_plant.production_plan GROUP BY shift_code ORDER BY shift_code
```

2 rows, 167 ms.

| shift_code | planned | built | attainment_pct | downtime_min |
|---|---|---|---|---|
| A | 24840 | 21023 | 84.6 | 4558.0 |
| B | 23400 | 20593 | 88.0 | 3313.0 |

## usl

The Unified Semantic Layer `floor` (namespace lightning.metastore.shopfloor), defined in app/usl.py.

Source: [`usl.sql`](usl.sql)

### Is the USL deployed, and is every table activated?

```sql
SHOW NAMESPACES OR TABLES IN lightning.metastore.shopfloor.floor
```

9 rows, 75 ms.

| col1 | col2 | col3 | col4 | col5 |
|---|---|---|---|---|
| agv | table |  | 0 | true |
| certification | table |  | 0 | true |
| incident | table |  | 0 | true |
| machine | table |  | 0 | true |
| safety_rule | table |  | 0 | true |
| station | table |  | 0 | true |
| vibration_sample | table |  | 0 | true |
| worker | table |  | 0 | true |
| zone | table |  | 0 | true |

### Which zones lose the most downtime? (incident -> station -> zone, following the declared foreign keys)

```sql
SELECT z.name AS zone, count(*) AS incidents, round(sum(i.downtime_min), 0) AS downtime_min
FROM lightning.metastore.shopfloor.floor.incident i
JOIN lightning.metastore.shopfloor.floor.station s ON i.station_id = s.station_id
JOIN lightning.metastore.shopfloor.floor.zone z ON s.zone_id = z.zone_id
GROUP BY z.name ORDER BY downtime_min DESC
```

6 rows, 669 ms.

| zone | incidents | downtime_min |
|---|---|---|
| Final assembly | 789 | 2930.0 |
| Body shop | 190 | 1651.0 |
| Trim line | 363 | 1376.0 |
| Marriage station | 226 | 914.0 |
| Paint shop | 130 | 637.0 |
| End-of-line test | 30 | 0.0 |

### Does the Bosch vibration label agree with the false-alarm flag? (sf_ops incident -> sf_plant recording, one query)

```sql
SELECT v.label, count(*) AS incidents, sum(CASE WHEN i.false_alarm THEN 1 ELSE 0 END) AS false_alarms
FROM lightning.metastore.shopfloor.floor.incident i
JOIN lightning.metastore.shopfloor.floor.vibration_sample v ON i.vibration_recording = v.recording
GROUP BY v.label ORDER BY v.label
```

2 rows, 527 ms.

| label | incidents | false_alarms |
|---|---|---|
| abnormal | 130 | 0 |
| normal | 20 | 20 |

### Which workers hold a lapsed certificate on the plant date? (sf_hr worker -> certification, by key)

```sql
SELECT w.worker_id, w.role, w.shift_code, c.cert, c.expires_date
FROM lightning.metastore.shopfloor.floor.worker w
JOIN lightning.metastore.shopfloor.floor.certification c ON c.worker_id = w.worker_id
WHERE c.expires_date < DATE '2026-10-16' ORDER BY c.expires_date DESC LIMIT 10
```

2 rows, 443 ms.

| worker_id | role | shift_code | cert | expires_date |
|---|---|---|---|---|
| W-22 | maintenance_tech | N | lockout_tagout | 2026-08-29 |
| W-21 | material_handler | B | forklift | 2026-08-14 |
