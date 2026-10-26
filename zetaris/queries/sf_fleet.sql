-- sf_fleet: the AGV fleet (Postgres database `fleet`). The candidates agent uses it to send only healthy robots.

-- Q: Is the fleet operable, and how healthy is each status group?
SELECT status, count(*) AS agvs, round(avg(battery_health_pct), 1) AS avg_battery_health_pct, sum(faults_90d) AS faults_90d
FROM sf_fleet.agvs GROUP BY status ORDER BY agvs DESC

-- Q: Which AGVs abort or delay the most tasks?
SELECT agv_id, count(*) AS tasks, sum(CASE WHEN status = 'aborted' THEN 1 ELSE 0 END) AS aborted,
       sum(CASE WHEN status = 'late' THEN 1 ELSE 0 END) AS late, round(avg(delay_s), 1) AS avg_delay_s
FROM sf_fleet.agv_tasks GROUP BY agv_id ORDER BY aborted DESC LIMIT 8

-- Q: Which fault codes does AGV telemetry report most, and on how many robots?
SELECT fault_code, count(*) AS readings, count(DISTINCT agv_id) AS agvs
FROM sf_fleet.agv_telemetry WHERE fault_code IS NOT NULL AND fault_code <> ''
GROUP BY fault_code ORDER BY readings DESC LIMIT 8
