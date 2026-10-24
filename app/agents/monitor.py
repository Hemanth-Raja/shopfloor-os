"""Monitor: classify an alarm as real or noise from thresholds and the Bosch vibration evidence.

The classification is rule-based. NEMOTRON_FAST is used only for a one-line human summary, and that is
optional: if Nightshift is unavailable, a rule-based summary is used instead.
"""

from __future__ import annotations

from typing import Callable

from app.contracts import Incident


def assess(incident: Incident, llm_text: Callable | None = None) -> dict:
    s = incident.sensors
    evidence: list[str] = []
    if incident.kind == "overheat":
        temp_over = s.get("process_temp_k", 0) > s.get("temp_threshold_k", 313.0)
        vib_abnormal = s.get("vibration_label") == "abnormal"
        evidence.append(f"process temp {s.get('process_temp_k')} K vs threshold {s.get('temp_threshold_k')} K")
        evidence.append(f"Bosch vibration {s.get('vibration_recording', 'n/a')}: {s.get('vibration_label')}")
        agreement = temp_over == vib_abnormal
        p_real = 0.92 if temp_over and vib_abnormal else 0.2 if temp_over else 0.05
    elif incident.kind == "spill":
        p_real, agreement = float(s.get("camera_confidence", 0.9)), True
        evidence.append(f"camera confidence {p_real}")
    elif incident.kind == "part_shortage":
        p_real, agreement = 0.95, True
        evidence.append(f"line-side stock {s.get('line_side_stock_min')} min")
    else:  # agv_fault
        p_real, agreement = 0.97, True
        evidence.append(f"AGV fault code {s.get('fault_code')}")
    verdict = "real" if p_real >= 0.5 else "likely_noise"
    summary = f"{incident.kind} at {incident.station_id or incident.zone_id}: {verdict} (p={p_real:.2f}); " + "; ".join(evidence)
    if llm_text is not None:
        try:
            reply = llm_text("fast", [{"role": "user", "content": "In one line (max 20 words), summarise for a shift "
                                       f"supervisor: {summary}"}], max_tokens=300)
            if reply.text.strip():
                summary = reply.text.strip().splitlines()[0]
        except Exception:  # noqa: BLE001 - the summary is optional; the classification stands without it
            pass
    return {"p_real": p_real, "verdict": verdict, "sensor_agreement": agreement, "evidence": evidence, "summary": summary}
