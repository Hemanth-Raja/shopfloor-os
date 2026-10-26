-- The Unified Semantic Layer `floor` (namespace lightning.metastore.shopfloor), defined in app/usl.py.

-- Q: Is the USL deployed, and is every table activated?
SHOW NAMESPACES OR TABLES IN lightning.metastore.shopfloor.floor

-- Q: Which zones lose the most downtime? (incident -> station -> zone, following the declared foreign keys)
SELECT z.name AS zone, count(*) AS incidents, round(sum(i.downtime_min), 0) AS downtime_min
FROM lightning.metastore.shopfloor.floor.incident i
JOIN lightning.metastore.shopfloor.floor.station s ON i.station_id = s.station_id
JOIN lightning.metastore.shopfloor.floor.zone z ON s.zone_id = z.zone_id
GROUP BY z.name ORDER BY downtime_min DESC

-- Q: Does the Bosch vibration label agree with the false-alarm flag? (sf_ops incident -> sf_plant recording, one query)
SELECT v.label, count(*) AS incidents, sum(CASE WHEN i.false_alarm THEN 1 ELSE 0 END) AS false_alarms
FROM lightning.metastore.shopfloor.floor.incident i
JOIN lightning.metastore.shopfloor.floor.vibration_sample v ON i.vibration_recording = v.recording
GROUP BY v.label ORDER BY v.label

-- Q: Which workers hold a lapsed certificate on the plant date? (sf_hr worker -> certification, by key)
SELECT w.worker_id, w.role, w.shift_code, c.cert, c.expires_date
FROM lightning.metastore.shopfloor.floor.worker w
JOIN lightning.metastore.shopfloor.floor.certification c ON c.worker_id = w.worker_id
WHERE c.expires_date < DATE '2026-10-16' ORDER BY c.expires_date DESC LIMIT 10
