-- sf_plant: stations, machines, telemetry, production (Postgres database `plant`). The monitor reads telemetry and Bosch vibration; the World Model reads the layout.

-- Q: How is the line laid out: stations per zone, kitting and the thinnest line-side buffer?
SELECT zone_id, count(*) AS stations, sum(CASE WHEN kitted THEN 1 ELSE 0 END) AS kitted, min(buffer_min) AS min_buffer_min
FROM sf_plant.stations GROUP BY zone_id ORDER BY zone_id

-- Q: Which machines fail most in their telemetry (AI4I-shaped readings)?
SELECT machine_id, count(*) AS readings, sum(failure) AS failures, sum(heat_dissipation_failure) AS heat_failures,
       sum(overstrain_failure) AS overstrain_failures
FROM sf_plant.machine_telemetry GROUP BY machine_id ORDER BY failures DESC LIMIT 8

-- Q: How close does each shift get to the production plan?
SELECT shift_code, sum(planned_units) AS planned, sum(built_units) AS built,
       round(100.0 * sum(built_units) / sum(planned_units), 1) AS attainment_pct, round(sum(downtime_min), 0) AS downtime_min
FROM sf_plant.production_plan GROUP BY shift_code ORDER BY shift_code
