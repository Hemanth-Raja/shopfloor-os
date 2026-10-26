-- sf_hr: people, certificates and rosters (Postgres database `hr`). The critic and the gate block anyone without an in-date certificate.

-- Q: Who works here, by role and shift?
SELECT role, shift_code, count(*) AS workers FROM sf_hr.workers GROUP BY role, shift_code ORDER BY role, shift_code

-- Q: Which certificates have lapsed on the plant date (16 Oct 2026)?
SELECT cert, count(*) AS held, sum(CASE WHEN expires_date < DATE '2026-10-16' THEN 1 ELSE 0 END) AS lapsed,
       min(expires_date) AS earliest_expiry
FROM sf_hr.certifications GROUP BY cert ORDER BY lapsed DESC, cert

-- Q: Who is rostered on the plant date, by shift?
SELECT shift_code, status, count(*) AS workers FROM sf_hr.shifts
WHERE shift_date = DATE '2026-10-16' GROUP BY shift_code, status ORDER BY shift_code, status
