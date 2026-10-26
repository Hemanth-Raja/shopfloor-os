COMPILE USL floor DEPLOY NAMESPACE lightning.metastore.shopfloor DDL
CREATE TABLE zone (
  zone_id varchar(20) NOT NULL PRIMARY KEY,
  name varchar(80),
  kind varchar(40)
);
CREATE TABLE station (
  station_id varchar(20) NOT NULL PRIMARY KEY,
  name varchar(80),
  zone_id varchar(20) FOREIGN KEY REFERENCES zone(zone_id),
  x int,
  y int,
  kitted boolean,
  buffer_min int
);
CREATE TABLE machine (
  machine_id varchar(20) NOT NULL PRIMARY KEY,
  name varchar(80),
  kind varchar(40),
  station_id varchar(20) FOREIGN KEY REFERENCES station(station_id),
  zone_id varchar(20) FOREIGN KEY REFERENCES zone(zone_id),
  commissioned_year int
);
CREATE TABLE worker (
  worker_id varchar(20) NOT NULL PRIMARY KEY,
  name varchar(80),
  role varchar(40),
  shift_code varchar(4),
  station_id varchar(20) FOREIGN KEY REFERENCES station(station_id),
  home_zone varchar(20) FOREIGN KEY REFERENCES zone(zone_id),
  skills varchar(200),
  forklift_licence boolean
);
CREATE TABLE certification (
  cert_id varchar(80) NOT NULL PRIMARY KEY,
  worker_id varchar(20) FOREIGN KEY REFERENCES worker(worker_id),
  cert varchar(40),
  issued_date date,
  expires_date date
);
CREATE TABLE agv (
  agv_id varchar(20) NOT NULL PRIMARY KEY,
  model varchar(80),
  kind varchar(40),
  status varchar(20),
  battery_pct int,
  battery_health_pct int,
  faults_90d int,
  last_service_date date
);
CREATE TABLE vibration_sample (
  recording varchar(80) NOT NULL PRIMARY KEY,
  bosch_machine varchar(20),
  label varchar(20),
  x_rms float,
  linked_machine_id varchar(20) FOREIGN KEY REFERENCES machine(machine_id)
);
CREATE TABLE safety_rule (
  rule_id varchar(40) NOT NULL PRIMARY KEY,
  cfr_ref varchar(80),
  check_type varchar(40),
  summary varchar(400)
);
CREATE TABLE incident (
  incident_id varchar(40) NOT NULL PRIMARY KEY,
  ts timestamp,
  shift_code varchar(4),
  kind varchar(40),
  cause varchar(200),
  zone_id varchar(20) FOREIGN KEY REFERENCES zone(zone_id),
  station_id varchar(20) FOREIGN KEY REFERENCES station(station_id),
  machine_id varchar(20) FOREIGN KEY REFERENCES machine(machine_id),
  agv_id varchar(20) FOREIGN KEY REFERENCES agv(agv_id),
  worker_id varchar(20) FOREIGN KEY REFERENCES worker(worker_id),
  vibration_recording varchar(80) FOREIGN KEY REFERENCES vibration_sample(recording),
  severity varchar(20),
  time_to_clear_s int,
  downtime_min float,
  cars_lost int,
  cost_usd int,
  outcome varchar(40),
  false_alarm boolean,
  description varchar(400)
)

ACTIVATE USL TABLE lightning.metastore.shopfloor.floor.zone AS SELECT COALESCE(zone_id, '') AS zone_id, name, kind FROM sf_ops.zones

ACTIVATE USL TABLE lightning.metastore.shopfloor.floor.station AS SELECT COALESCE(station_id, '') AS station_id, name, zone_id, x, y, kitted, buffer_min FROM sf_plant.stations

ACTIVATE USL TABLE lightning.metastore.shopfloor.floor.machine AS SELECT COALESCE(machine_id, '') AS machine_id, name, kind, station_id, zone_id, commissioned_year FROM sf_plant.machines

ACTIVATE USL TABLE lightning.metastore.shopfloor.floor.worker AS SELECT COALESCE(worker_id, '') AS worker_id, name, role, shift_code, station_id, home_zone, skills, forklift_licence FROM sf_hr.workers

ACTIVATE USL TABLE lightning.metastore.shopfloor.floor.certification AS SELECT COALESCE(concat(worker_id, ':', cert), '') AS cert_id, worker_id, cert, issued_date, expires_date FROM sf_hr.certifications

ACTIVATE USL TABLE lightning.metastore.shopfloor.floor.agv AS SELECT COALESCE(agv_id, '') AS agv_id, model, kind, status, battery_pct, battery_health_pct, faults_90d, last_service_date FROM sf_fleet.agvs

ACTIVATE USL TABLE lightning.metastore.shopfloor.floor.vibration_sample AS SELECT COALESCE(recording, '') AS recording, bosch_machine, label, x_rms, linked_machine_id FROM sf_plant.vibration_samples

ACTIVATE USL TABLE lightning.metastore.shopfloor.floor.safety_rule AS SELECT COALESCE(rule_id, '') AS rule_id, cfr_ref, check_type, summary FROM sf_ops.safety_rules

ACTIVATE USL TABLE lightning.metastore.shopfloor.floor.incident AS SELECT COALESCE(incident_id, '') AS incident_id, ts, shift_code, kind, cause, zone_id, station_id, machine_id, agv_id, worker_id, vibration_recording, severity, time_to_clear_s, downtime_min, cars_lost, cost_usd, outcome, false_alarm, description FROM sf_ops.incidents
