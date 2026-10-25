"""Nemotron at work: what Nemotron did in this system, counted from the logs, not claimed.

Two sources. logs/llm.jsonl has one line per answered Nemotron call (role lane, model asked, model that answered,
latency, tokens). The incident traces say what each answer decided: router hops Nemotron chose versus hops code
forced or the rule router chose, plans Nemotron wrote, unsafe plans the safety critic sent back and Nemotron then
fixed, and incidents that ran in safe mode because no model answered. No model call here.
"""

from __future__ import annotations

import json
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from app.models import CFG, effective_chain

LLM_LOG = Path(__file__).resolve().parents[1] / "logs" / "llm.jsonl"
LANE_ROLE = {"deep": "planner", "fast": "monitor"}  # older lines name the lane, not the role


def _pct(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(q * len(xs)))], 1)


def calls(path: Path = LLM_LOG, hours: float | None = 24, now: datetime | None = None) -> dict:
    """Per role: calls, which model answered, latency, how often the first-choice model was not the one used."""
    cutoff = (now or datetime.now().astimezone()) - timedelta(hours=hours) if hours else None
    by: dict[str, list[dict]] = defaultdict(list)
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                r = json.loads(line)
                if cutoff and datetime.fromisoformat(r["ts"]) < cutoff:
                    continue
            except (ValueError, KeyError):
                continue
            by[LANE_ROLE.get(r.get("lane"), r.get("lane") or "?")].append(r)
    roles, total_tokens = [], 0
    for role, rows in sorted(by.items(), key=lambda kv: -len(kv[1])):
        chain = effective_chain(role) if role in CFG["roles"] else []  # minus models the self-check skipped
        lat = [float(r.get("latency_s") or 0) for r in rows]
        tokens = sum(int((r.get("tokens") or {}).get("total") or 0) for r in rows)
        total_tokens += tokens
        models = Counter(str(r.get("answering_model", "?")).replace("nvidia/", "") for r in rows)
        roles.append({"role": role, "calls": len(rows), "models": dict(models.most_common()),
                      "p50_s": _pct(lat, 0.5), "p95_s": _pct(lat, 0.95), "tokens": tokens,
                      "fallback": sum(1 for r in rows if chain and r.get("requested_model") != chain[0]),
                      "served_other": sum(1 for r in rows if r.get("answering_model") != r.get("requested_model")),
                      "truncated": sum(1 for r in rows if r.get("truncated")),
                      "why": (CFG["roles"].get(role) or {}).get("why", "")})
    return {"hours": hours, "calls": sum(r["calls"] for r in roles), "tokens": total_tokens, "roles": roles}


def _why_v1(d: dict) -> str:
    """Why the checked v1 stayed. An optimal assignment is hard to beat on its own score, so Nemotron reaching the
    same team on its own is agreement, not a loss."""
    v1, v2 = d.get("v1") or {}, d.get("v2") or {}
    if v2.get("violations") or v2.get("score") is None:
        return "v2_rejected"  # broke a safety rule or failed the dry run
    if sorted(v1.get("assignees") or []) == sorted(v2.get("assignees") or []):
        return "v2_agreed"
    return "v2_same_score" if v1.get("score") == v2.get("score") else "v1_better"


def decisions(traces) -> dict:
    """What the answers decided, across the incidents this server handled."""
    hops = Counter()
    plans = Counter()
    refine = Counter()
    receipts = Counter()
    rejected = fixed = safe = unsafe = caught = 0
    for t in traces:
        for p in t.path or []:
            by = str(p.get("by") or "")
            hops["nemotron" if by.startswith("nemotron") else by or "rules"] += 1
        critic = [st for st in t.steps if st.step == "critic"]
        bad = [i for i, st in enumerate(critic) if not st.ok]
        caught += any("nemotron" in str(critic[i].detail.get("plan_source") or "") for i in bad)
        if bad:
            rejected += 1
            if any(st.ok and "nemotron" in str(st.detail.get("plan_source") or "") for st in critic[bad[0] + 1:]):
                fixed += 1
        for st in t.steps:
            if st.step == "refine":  # anytime planning: rules plan v1 at once, Nemotron's v2 replaces it only if better
                if st.detail.get("model_call") or st.detail.get("model"):
                    refine["nemotron_v2"] += 1
                    refine["v2_ran" if st.detail.get("chosen") == "v2" else "v1_kept"] += 1
                    if st.detail.get("chosen") != "v2":
                        refine[_why_v1(st.detail)] += 1
                else:
                    refine["no_model"] += 1
            if st.step == "planner":
                plans["nemotron" if "nemotron" in str(st.detail.get("model") or st.detail.get("source") or "")
                      or st.detail.get("model_call") else "rules or playbook"] += 1
        for st in t.steps:  # receipts checked one by one, at the moment each answer was used
            for c in (st.detail.get("model_call"), (st.detail.get("route") or {}).get("model_call")):
                if isinstance(c, dict) and c.get("receipt") and c.get("receipt_verified") is not None:
                    receipts["checked"] += 1
                    receipts["verified"] += bool(c.get("receipt_verified"))
        safe += bool(t.safe_mode)
        ran = getattr(t, "gate", None) in ("auto_executed", "approved", "auto_after_timeout", "confirmed")
        # dispatched without any plan passing the critic (a rejected Nemotron v2 keeps the checked v1: that is safe)
        unsafe += bool(ran and not any(st.ok for st in critic))
    return {"incidents": len(traces), "hops": dict(hops), "plans": dict(plans), "critic_rejected": rejected,
            "fixed_by_nemotron": fixed, "safe_mode": safe, "unsafe_executed": unsafe,
            "nemotron_plans_caught": caught, "refine": dict(refine),
            "receipts_at_call": dict(receipts)}


def scorecard(traces, receipts: dict | None = None, path: Path = LLM_LOG, hours: float | None = 24) -> dict:
    c = calls(path, hours)
    lat = [r["p50_s"] for r in c["roles"] if r["p50_s"] is not None]
    return {**c, "median_of_role_p50_s": round(statistics.median(lat), 1) if lat else None,
            "decisions": decisions(traces), "receipts": receipts or {}}
