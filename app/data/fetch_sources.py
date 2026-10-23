"""Optional rebuild step: download the real-world sources and write the small, committed
summaries in data/seed/ that the generator reads. Judges and normal runs never need this.

    pip install -r requirements-data.txt      # h5py, only for the Bosch HDF5 files
    python -m app.data.fetch_sources           # all sources
    python -m app.data.fetch_sources bosch     # just one: ai4i, osha, bls, weather, bosch, mapf

Writes (committed):
  data/seed/ai4i2020.csv                     UCI AI4I 2020 Predictive Maintenance (CC BY 4.0), unchanged.
  data/seed/osha_sir_naics3361_counts.csv    OSHA Severe Injury Reports (public domain), NAICS 3361 only,
                                             aggregated to counts by event type. Employer names, addresses,
                                             locations and narratives are never written.
  data/seed/bls_soii_naics3361.csv           BLS SOII injury and illness rates for NAICS 3361 (public domain).
  data/seed/weather_detroit_hourly.csv       Open-Meteo hourly temperature for Detroit, MI over the simulation
                                             window. Weather data by Open-Meteo.com (CC BY 4.0).
  data/seed/bosch_cnc_vibration_summary.csv  Bosch CNC Machining dataset (CC BY 4.0): one row of vibration
                                             statistics per recording; the raw HDF5 files are not kept.
  data/seed/mapf/*.map                       MovingAI MAPF warehouse maps (Open Data Commons Attribution).
Writes (git-ignored, optional):
  data/raw/mapf/*.scen                       MovingAI MAPF scenarios for path-planner benchmarks.
"""

import csv
import io
import json
import random
import sys
import urllib.request
import zipfile
from collections import Counter, defaultdict
from datetime import timedelta
from pathlib import Path

from app.data.generate import DAYS, RAW, SEED, START

AI4I_URL = "https://archive.ics.uci.edu/static/public/601/ai4i+2020+predictive+maintenance+dataset.zip"
OSHA_URL = "https://www.osha.gov/sites/default/files/January2015toNovember2025.zip"
BLS_URL = "https://api.bls.gov/publicAPI/v1/timeseries/data/{}"
BLS_SERIES = {"ISUMFG33610031100": "total_recordable_rate_per_100_fte",
              "ISUMFG33610032100": "dart_rate_per_100_fte"}
METEO_URL = ("https://archive-api.open-meteo.com/v1/archive?latitude=42.33&longitude=-83.05"
             "&start_date={}&end_date={}&hourly=temperature_2m&timezone=America%2FDetroit")
BOSCH_TREE = "https://api.github.com/repos/boschresearch/CNC_Machining/git/trees/main?recursive=1"
BOSCH_RAW = "https://raw.githubusercontent.com/boschresearch/CNC_Machining/main/{}"
BOSCH_NORMAL_SAMPLE = 280  # all abnormal recordings plus a stratified sample of normal ones
BOSCH_HZ = 2000
MAPF_URL = "https://movingai.com/benchmarks/mapf/{}"
MAPF_MAPS = ["warehouse-10-20-10-2-1", "warehouse-10-20-10-2-2", "warehouse-20-40-10-2-1", "warehouse-20-40-10-2-2"]
HEADERS = {"User-Agent": "Mozilla/5.0 (shopfloor-os hackathon data fetch)", "Accept": "*/*"}

# OSHA event titles grouped into the hazard groups the generator uses.
GROUPS = [
    ("vehicle_pedestrian", ("vehicle", "forklift", "struck by powered", "nonroadway", "pedestrian")),
    ("caught_in_machinery", ("caught in running equipment", "compressed or pinched", "caught in or compressed")),
    ("slip_trip_fall", ("fall on same level", "slip", "trip")),
    ("fall_from_height", ("fall to lower level", "fall from", "fall through")),
    ("heat_exposure", ("heat",)),
    ("struck_by_object", ("struck by", "swinging object", "falling object", "flying object")),
]


def get(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=300) as r:
        return r.read()


def group_of(event_title):
    t = event_title.lower()
    return next((g for g, keys in GROUPS if any(k in t for k in keys)), "other")


def fetch_ai4i():
    archive = zipfile.ZipFile(io.BytesIO(get(AI4I_URL)))
    (SEED / "ai4i2020.csv").write_bytes(archive.read("ai4i2020.csv"))
    print(f"AI4I: {sum(1 for _ in (SEED / 'ai4i2020.csv').open()) - 1} rows -> ai4i2020.csv")


def fetch_osha():
    archive = zipfile.ZipFile(io.BytesIO(get(OSHA_URL)))
    name = next(n for n in archive.namelist() if n.lower().endswith(".csv"))
    counts, hosp, amp = Counter(), defaultdict(float), defaultdict(float)
    years = Counter()
    with archive.open(name) as f:
        for row in csv.DictReader(io.TextIOWrapper(f, encoding="latin-1", newline="")):
            if not str(row.get("Primary NAICS", "")).startswith("3361"):
                continue
            title = " ".join((row.get("EventTitle") or "unspecified").split())
            key = (group_of(title), title)
            counts[key] += 1
            hosp[key] += float(row.get("Hospitalized") or 0)
            amp[key] += float(row.get("Amputation") or 0)
            years[(row.get("EventDate") or "")[-4:]] += 1
    path = SEED / "osha_sir_naics3361_counts.csv"
    with path.open("w", newline="") as out:
        w = csv.writer(out)
        w.writerow(["hazard_group", "event_title", "reports", "hospitalized", "amputations"])
        for (g, title), n in sorted(counts.items(), key=lambda kv: (kv[0][0], -kv[1], kv[0][1])):
            w.writerow([g, title, n, int(hosp[(g, title)]), int(amp[(g, title)])])
    by_group = Counter()
    for (g, _), n in counts.items():
        by_group[g] += n
    print(f"OSHA: {sum(counts.values())} NAICS 3361 reports ({min(y for y in years if y)}-{max(years)}) "
          f"-> {path.name}: {dict(by_group.most_common())}")


def fetch_bls():
    path = SEED / "bls_soii_naics3361.csv"
    with path.open("w", newline="") as out:
        w = csv.writer(out)
        w.writerow(["series_id", "measure", "year", "value"])
        for sid, measure in BLS_SERIES.items():
            body = json.loads(get(BLS_URL.format(sid)))
            if body.get("status") != "REQUEST_SUCCEEDED":
                raise RuntimeError(f"BLS {sid}: {body.get('message')}")
            for d in body["Results"]["series"][0]["data"]:
                w.writerow([sid, measure, d["year"], d["value"]])
                print(f"BLS: {measure} {d['year']} = {d['value']}")


def fetch_weather():
    first, last = START.date(), (START + timedelta(days=DAYS - 1)).date()
    hourly = json.loads(get(METEO_URL.format(first, last)))["hourly"]
    path = SEED / "weather_detroit_hourly.csv"
    with path.open("w", newline="") as out:
        w = csv.writer(out)
        w.writerow(["ts", "temperature_c"])
        w.writerows(zip(hourly["time"], hourly["temperature_2m"]))
    temps = [t for t in hourly["temperature_2m"] if t is not None]
    print(f"Open-Meteo: {len(temps)} hourly temperatures {first}..{last}, {min(temps)}..{max(temps)} C -> {path.name}")


def vibration_stats(signal):
    """Per-axis statistics of a (samples, 3) vibration array, with the static offset removed."""
    import numpy as np
    centred = signal - signal.mean(axis=0)
    rms = np.sqrt((centred ** 2).mean(axis=0))
    peak = np.abs(centred).max(axis=0)
    kurtosis = (centred ** 4).mean(axis=0) / np.maximum(rms ** 4, 1e-12)
    out = []
    for axis in range(3):
        out += [round(float(rms[axis]), 2), round(float(peak[axis]), 1),
                round(float(peak[axis] / max(rms[axis], 1e-9)), 2), round(float(kurtosis[axis]), 2)]
    return out


def fetch_bosch():
    import h5py  # only this step needs it: pip install -r requirements-data.txt
    tree = json.loads(get(BOSCH_TREE))["tree"]
    files = sorted(t["path"] for t in tree if t["type"] == "blob" and t["path"].endswith(".h5"))
    abnormal = [p for p in files if "/bad/" in p]
    normal = [p for p in files if "/good/" in p]
    by_group = defaultdict(list)
    for p in normal:
        by_group[tuple(p.split("/")[1:3])].append(p)  # (machine, operation)
    pick = random.Random(7)
    chosen = []
    for group, paths in sorted(by_group.items()):
        k = max(1, round(BOSCH_NORMAL_SAMPLE * len(paths) / len(normal)))
        chosen += pick.sample(paths, min(k, len(paths)))
    rows = []
    for n, p in enumerate(sorted(abnormal + chosen), 1):
        _, machine, operation, label, name = p.split("/")
        with h5py.File(io.BytesIO(get(BOSCH_RAW.format(p))), "r") as f:
            signal = f["vibration_data"][()]
        period = "_".join(name.split("_")[1:3])
        rows.append([f"{Path(name).stem}_{label}", machine,  # Bosch restarts numbering per good/bad folder operation, period, "abnormal" if label == "bad" else "normal",
                     round(len(signal) / BOSCH_HZ, 1), *vibration_stats(signal)])
        if n % 50 == 0:
            print(f"Bosch: {n} recordings summarised")
    path = SEED / "bosch_cnc_vibration_summary.csv"
    with path.open("w", newline="") as out:
        w = csv.writer(out)
        w.writerow(["recording", "bosch_machine", "operation", "period", "label", "duration_s",
                    *[f"{a}_{s}" for a in "xyz" for s in ("rms", "peak", "crest", "kurtosis")]])
        w.writerows(rows)
    labels = Counter(r[4] for r in rows)
    print(f"Bosch: {len(rows)} recordings ({dict(labels)}) from {len(files)} available -> {path.name}")


def fetch_mapf():
    maps, scens = SEED / "mapf", RAW / "mapf"
    maps.mkdir(parents=True, exist_ok=True)
    scens.mkdir(parents=True, exist_ok=True)
    for name in MAPF_MAPS:
        for suffix in (".map.zip", ".map-scen-random.zip", ".map-scen-even.zip"):
            archive = zipfile.ZipFile(io.BytesIO(get(MAPF_URL.format(name + suffix))))
            for member in archive.namelist():
                base = Path(member).name
                if base.endswith(".map"):
                    (maps / base).write_bytes(archive.read(member))
                elif base.endswith(".scen"):
                    (scens / base).write_bytes(archive.read(member))
    print(f"MovingAI MAPF: {len(list(maps.glob('*.map')))} warehouse maps -> data/seed/mapf/, "
          f"{len(list(scens.glob('*.scen')))} scenarios -> data/raw/mapf/")


SOURCES = {"ai4i": fetch_ai4i, "osha": fetch_osha, "bls": fetch_bls, "weather": fetch_weather,
           "bosch": fetch_bosch, "mapf": fetch_mapf}


def main(names):
    SEED.mkdir(parents=True, exist_ok=True)
    RAW.mkdir(parents=True, exist_ok=True)
    for name in names or SOURCES:
        try:
            SOURCES[name]()
        except Exception as error:  # each source is optional; the committed seed file stays as it was
            print(f"{name}: failed ({str(error)[:200]}); keeping the existing data/seed file")


if __name__ == "__main__":
    main(sys.argv[1:])
