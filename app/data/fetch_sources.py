"""Download the real-world anchors for the generator into data/raw/ (git-ignored).

    python -m app.data.fetch_sources          # all three; each one is optional for the generator

Writes:
  data/raw/osha_sir_naics3361_counts.csv  OSHA Severe Injury Reports (public domain), NAICS 3361 only,
                                           aggregated to counts by event type. Employer names,
                                           addresses, locations and narratives are never written.
  data/raw/bls_soii_naics3361.csv         BLS SOII injury and illness rates for NAICS 3361 (public domain).
  data/raw/weather_detroit_hourly.csv     Open-Meteo historical hourly temperature for Detroit, MI
                                           over the simulation window. Weather data by Open-Meteo.com (CC BY 4.0).

Standard library only.
"""

import csv
import io
import json
import urllib.request
import zipfile
from collections import Counter, defaultdict
from datetime import timedelta

from app.data.generate import DAYS, RAW, START

OSHA_URL = "https://www.osha.gov/sites/default/files/January2015toNovember2025.zip"
BLS_URL = "https://api.bls.gov/publicAPI/v1/timeseries/data/{}"
BLS_SERIES = {"ISUMFG33610031100": "total_recordable_rate_per_100_fte",
              "ISUMFG33610032100": "dart_rate_per_100_fte"}
METEO_URL = ("https://archive-api.open-meteo.com/v1/archive?latitude=42.33&longitude=-83.05"
             "&start_date={}&end_date={}&hourly=temperature_2m&timezone=America%2FDetroit")
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
    path = RAW / "osha_sir_naics3361_counts.csv"
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
    path = RAW / "bls_soii_naics3361.csv"
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
    path = RAW / "weather_detroit_hourly.csv"
    with path.open("w", newline="") as out:
        w = csv.writer(out)
        w.writerow(["ts", "temperature_c"])
        w.writerows(zip(hourly["time"], hourly["temperature_2m"]))
    temps = [t for t in hourly["temperature_2m"] if t is not None]
    print(f"Open-Meteo: {len(temps)} hourly temperatures {first}..{last}, {min(temps)}..{max(temps)} C -> {path.name}")


def main():
    RAW.mkdir(parents=True, exist_ok=True)
    for name, fn in [("OSHA", fetch_osha), ("BLS", fetch_bls), ("Open-Meteo", fetch_weather)]:
        try:
            fn()
        except Exception as error:  # each source is optional; the generator falls back without it
            print(f"{name}: download failed ({str(error)[:200]}); the generator will use its fallback")


if __name__ == "__main__":
    main()
