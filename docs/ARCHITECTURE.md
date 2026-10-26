# Architecture

An index of how Shop-Floor OS is built. Part of the [README](../README.md#how-it-works).

![Architecture: people use four agent teams, each with its feedback loops; the teams use the Meterless engines, every plan goes through deterministic safety code before the simulated floor moves, data comes from Zetaris and every model call goes through Meterless Nightshift to NVIDIA Nemotron](architecture.png)

<sub>Source: [architecture.html](architecture.html).</sub>

## How the agents work together
There is no fixed chain. In an incident the orchestrator ([`app/dynamic.py`](../app/dynamic.py)) picks the next agent among the
moves code allows, and agents send work back when a check fails. Each arrow is labelled with its condition.

```mermaid
flowchart TB
  ZT[("Zetaris: sf_fleet, sf_hr, sf_plant, sf_ops")]
  WM[("World Model")]
  HM[("H-MEM lessons and playbooks")]
  O{{"Orchestrator: picks the next agent among the moves code allows"}}
  MON["Monitor"]
  MEM["Memory"]
  CAND["Candidates"]
  PLAN["Planner: v1 at once, Nemotron v2"]
  CRIT["Safety critic"]
  DRY["Dry run"]
  RG["Restraint and gate"]
  EXE["Executor"]
  VER["Verifier"]
  PER(["Person"])
  FEN["Area stays fenced"]
  ARB["Arbiter and leases"]
  ZT -->|"one federated query"| WM
  O <-->|"legal move, result"| MON
  O <-->|"legal move, result"| MEM
  O <-->|"legal move, result"| CAND
  O <-->|"legal move, result"| PLAN
  HM -->|"lessons, proven playbook"| MEM
  WM -->|"qualified people, healthy AGVs"| CAND
  WM -->|"rejects invented ids"| PLAN
  CAND --> PLAN
  PLAN -->|"plan"| CRIT
  CRIT -->|"rejected: the clause goes back, up to 3 tries"| PLAN
  CRIT -->|"rejected: re-pick people and robots"| CAND
  CRIT -->|"violation becomes a lesson"| HM
  CRIT -->|"passed"| DRY
  DRY -->|"Nemotron v2 collides or is late: keep v1"| PLAN
  DRY -->|"checked"| RG
  RG -->|"routine ACT"| EXE
  RG -->|"hazard, lapsed certificate, failed dry run, ESCALATE"| PER
  PER -->|"approve"| EXE
  PER -->|"reject"| FEN
  EXE -->|"lease request"| ARB
  ARB -->|"taken by a more urgent incident: re-plan without it"| CAND
  EXE --> VER
  VER -->|"floor not as predicted: re-plan without the failed AGV, up to 3 rounds"| CAND
  VER -->|"outcome raises or lowers confidence"| HM
```

The other three teams each run their own loop and meet the incident loop at the orchestrator, Zetaris and the
World Model:

```mermaid
flowchart TB
  ZT[("Zetaris")]
  WM[("World Model")]
  O{{"Incident orchestrator"}}
  subgraph AUTO["Autopilot"]
    SEN["Sentinel"] -->|"raises an incident"| DISP["Dispatcher: priority queue"]
  end
  subgraph ASK["Ask the floor"]
    CO["Coordinator"] -->|"sub-tasks"| SQL["SQL agent"]
    CO -->|"questions about the agents"| LOGA["Log agent"]
    SQL -->|"query"| GRD["SQL guard"]
    GRD -->|"refused: the reason goes back, 1 retry"| SQL
    GRD -->|"allowed: rows"| AN["Analyst"]
    LOGA -->|"traces and lessons"| AN
    AN -->|"findings with evidence ids"| EC["Evidence critic"]
    EC -->|"claim not backed by the rows: dropped"| AN
  end
  subgraph LAB["Lab in the loop"]
    HYP["Hypothesis: Nemotron"] -->|"one floor change"| REP["20 seeded replays, bootstrap CI"]
    REP -->|"significant"| APP(["Person saves and applies"])
    APP --> MEAS["Later incidents measured"]
    MEAS -->|"predicted vs measured"| HYP
    REP -->|"every run is listed for the next hypothesis"| HYP
  end
  DISP -->|"one orchestrator per incident, most urgent first"| O
  O -->|"each finished incident"| MEAS
  GRD <-->|"read-only SQL"| ZT
  WM -->|"floor facts"| AN
  ZT -->|"incident history"| HYP
```

## Where to read more
| Page | What it covers |
|---|---|
| [agents.md](agents.md) | every agent, the legal moves code allows, Ask the floor, Autopilot (Sentinel, dispatcher, leases), autonomy levels |
| [reliability.md](reliability.md) | retrieval without a vector database, context limits, failure handling, resilience, what broke during the build |
| [production-readiness.md](production-readiness.md) | what a real deployment would still need |
| [models-and-engines.md](models-and-engines.md) | which Nemotron model serves which role; how the Meterless engines are ported and tested |
| [data.md](data.md) | the fictional plant, what is real and synthetic, the planted patterns |
| [eval.md](eval.md), [chaos.md](chaos.md) | the safety evaluation and the 15 failure scenarios |
| [sponsors/](sponsors/README.md) | [Zetaris](sponsors/ZETARIS.md), [Meterless](sponsors/METERLESS.md), [NVIDIA](sponsors/NVIDIA.md), [Cursor](sponsors/CURSOR.md) |
| [USER_GUIDE.md](USER_GUIDE.md) | the web UI, tab by tab |
