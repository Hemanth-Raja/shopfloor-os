-- sf_ops: incidents, andon pulls, safety rules, the floor grid, and the agents' own logs (Postgres database `ops`).

-- Q: Which incident kinds cost the most, and how many are false alarms?
SELECT kind, count(*) AS incidents, round(100.0 * sum(CASE WHEN false_alarm THEN 1 ELSE 0 END) / count(*), 1) AS false_alarm_pct,
       round(sum(downtime_min), 0) AS downtime_min, sum(cars_lost) AS cars_lost
FROM sf_ops.incidents GROUP BY kind ORDER BY downtime_min DESC

-- Q: Which andon reasons stop the line, and how fast does someone respond?
SELECT reason, count(*) AS pulls, sum(CASE WHEN stopped_line THEN 1 ELSE 0 END) AS stopped_line,
       round(avg(response_s), 0) AS avg_response_s
FROM sf_ops.andon_events GROUP BY reason ORDER BY pulls DESC

-- Q: Which rules does the safety critic reject plans for most often (the agents' own steps, synced from logs/)?
SELECT rules, count(*) AS rejections FROM sf_ops.agent_steps
WHERE agent = 'critic' AND ok = false AND rules IS NOT NULL AND rules <> ''
GROUP BY rules ORDER BY rejections DESC LIMIT 8
