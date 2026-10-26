-- Zetaris data quality on the USL: the rule `cert_in_date`, registered by `python -m app.usl dq` (REGISTER DQ is an admin write; these two reads are not).

-- Q: Which key constraints and data-quality rules does Zetaris hold for the USL?
LIST DQ USL lightning.metastore.shopfloor.floor

-- Q: Which certificates fail the rule `cert_in_date` (not in date on 16 Oct 2026)?
SHOW DQ INVALID RECORD cert_in_date TABLE lightning.metastore.shopfloor.floor.certification
