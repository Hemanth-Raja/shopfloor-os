"""Pydantic contracts the agents exchange. Strict: unknown fields are rejected (AGENTS.md rule 6)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

IncidentKind = Literal["part_shortage", "agv_fault", "overheat", "spill",
                       "robot_cell_fault", "torque_fault", "near_miss", "quality_defect"]
Decision = Literal["ACT", "SAFE_HOLD", "VERIFY", "ESCALATE", "DO_NOTHING"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Incident(Strict):
    id: str
    kind: IncidentKind
    station_id: str | None = None
    zone_id: str
    x: int
    y: int
    tick: int
    severity: Literal["low", "medium", "high", "critical"] = "medium"
    agv_id: str | None = None
    machine_id: str | None = None
    note: str | None = Field(default=None, max_length=300)  # free text from the person who reported it
    sensors: dict[str, Any] = Field(default_factory=dict)  # e.g. process_temp_k, vibration_recording, vibration_label
    truth_false_alarm: bool | None = Field(default=None, exclude=True)  # sim ground truth, never shown to agents


class Candidate(Strict):
    id: str  # world-model id, e.g. worker:plant:W-03 or agv:plant:AGV-4
    type: Literal["worker", "agv"]
    name: str
    distance: int  # grid cells (Manhattan) from the incident
    skills: list[str] = Field(default_factory=list)
    certs_valid: dict[str, bool] = Field(default_factory=dict)
    health: dict[str, Any] = Field(default_factory=dict)  # AGV: battery_health_pct, faults_90d, service_overdue
    score: float
    reasons: list[str] = Field(default_factory=list)


class AgvRoute(Strict):
    agv_id: str
    to_station: str
    speed_mps: float = Field(gt=0, le=3)
    crossing_speed_mps: float | None = Field(default=None, gt=0, le=3)


class Plan(Strict):
    incident_id: str
    assignee_ids: list[str] = Field(min_length=1)
    agv_routes: list[AgvRoute] = Field(default_factory=list)
    priority: Literal["low", "medium", "high", "critical"]
    rationale: str = Field(min_length=1, max_length=600)
    citations: list[str] = Field(default_factory=list)  # refs of the clauses the plan follows


class Citation(Strict):
    ref: str  # e.g. "29 CFR 1910.178(n)(4)" or "SOP-AGV-01 §2"
    title: str
    text: str  # verbatim clause text
    doc: Literal["osha", "sop"]


class Violation(Strict):
    rule_id: str
    cfr_ref: str
    detail: str
    citations: list[Citation] = Field(default_factory=list)


class CriticVerdict(Strict):
    ok: bool
    violations: list[Violation] = Field(default_factory=list)


class RestraintDecision(Strict):
    decision: Decision
    score: float
    reasons: list[str]


class StepResult(Strict):
    step: str
    ok: bool
    ms: float
    detail: dict[str, Any] = Field(default_factory=dict)


class Trace(Strict):
    incident: Incident
    steps: list[StepResult] = Field(default_factory=list)
    plan: Plan | None = None
    decision: RestraintDecision | None = None
    critic_rejections: int = 0
    replans: int = 0
    safe_mode: bool = False
    autonomy: Literal["auto", "supervised", "advisory"] = "auto"
    gate: Literal["none", "auto_executed", "awaiting_approval", "advisory", "approved", "rejected", "rolled_back"] = "none"
    gate_reasons: list[str] = Field(default_factory=list)
    dispatch: dict[str, list[int]] = Field(default_factory=dict)  # agent -> cell it started from (for roll-back)
