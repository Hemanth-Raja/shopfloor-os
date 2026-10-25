"""Intake: what is the person actually asking? Runs before the coordinator splits anything into SQL.

"good morning" is not a question about downtime, "there's a spill at F20" is a report for the orchestrator, "delete
the incidents" must be refused, and "and F20?" only makes sense next to the last question. Code settles the clear
cases (greetings, thanks, help, emergencies, write and action requests, follow-ups, plain floor questions) without a
model; only what is left goes to the coordinator model, whose reply is validated (Intake schema, known stations and
kinds). The non-investigation intents are answered from the orchestrator's live floor (`live`), so a greeting gets a
short shift briefing instead of a table.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

INVESTIGATE = {"floor_question", "follow_up"}  # the intents that go on to the coordinator and the SQL agents

Kind = Literal["greeting", "thanks", "help", "live_status", "floor_question", "follow_up", "report", "emergency",
               "action", "write", "off_topic", "unclear"]


class Intake(BaseModel):
    model_config = ConfigDict(extra="forbid")
    intent: Kind
    question: str = Field(default="", max_length=400)  # the standalone question to investigate, if any
    reply: str = Field(default="", max_length=600)  # what to say for the intents that are not investigated
    options: list[str] = Field(default_factory=list, max_length=4)  # clarifying choices or questions to try next
    kind: str | None = None  # report: the incident kind, if one fits
    station: str | None = None  # report: the station named


SUGGESTED = ["Why does F10 keep stopping?", "Which AGV is least reliable and when?", "Are overheat alarms real?",
             "Which shift has the most part shortages at F30?"]

DOMAIN = re.compile(r"\b(?:[btpmfe]\d0|agv-?\d?|agvs?|robots?|stations?|incidents?|downtime|stops?|stopp\w*|shifts?|"
                    r"workers?|operators?|crew|people|machines?|overheat\w*|spills?|shortages?|parts?|kits?|cert\w*|"
                    r"battery|batteries|alarms?|faults?|torque|defects?|quality|near.?miss\w*|line|cars?|cost\w*|"
                    r"delays?|clear\w*|playbooks?|lessons?|decisions?|approv\w*|critic|planner|agents?|nemotron|zones?|"
                    r"crossings?|conveyor|paint|weld\w*|safety|rules?|reliab\w*|vibration|temperature|hall|floor|output|throughput|"
                    r"production|jph|oee|uptime|efficien\w*|performance|losses|lost)\b")
GREETING = re.compile(r"^(?:hi|hii+|hello|hey|hiya|howdy|yo|greetings|good\s+(?:morning|afternoon|evening|day)|morning|"
                      r"evening|afternoon)\b[\s,!.]*")
FILLER = re.compile(r"\b(?:there|team|all|everyone|everybody|folks|guys|claude|floor|agents?|bot|again|to you|you|"
                    r"how are you|how's it going|hows it going|how is it going|what's up|whats up|sup)\b")
THANKS = re.compile(r"^(?:thanks?|thank you|thx|ty|cheers|ok|okay|cool|great|nice|perfect|got it|bye|goodbye|"
                    r"see you|good night|later)\b")
HELP = re.compile(r"\b(?:what can (?:you|i) (?:do|ask)|what do you do|help\b|how does this work|how do i use|who are you|"
                  r"what are you|what is this|what should i ask|examples?)\b")
EMERGENCY = re.compile(r"\b(?:fire|smoke|smoking|flames?|burning|injur\w*|hurt|bleed\w*|unconscious|collaps\w*|"
                       r"trapped|ambulance|evacuat\w*|explosion|electrocut\w*|accident)\b")
WRITE = re.compile(r"\b(?:delete|truncate|insert into|wipe|erase|purge|overwrite)\b|\bdrop (?:table|the|all|database|schema)\b|"
                   r"\b(?:update|modify|edit|change|remove|reset|clear|alter)\b.{0,40}\b(?:data|incidents?|records?|rows?|"
                   r"tables?|history|logs?|database|values?|columns?)\b")  # "any update on F10?" / "why did output drop" pass
ACTION = re.compile(r"^(?:please\s+|can you\s+|could you\s+)?(?:send|dispatch|move|stop|start|restart|approve|reject|"
                    r"park|charge|assign|shut|halt|reroute|call|fix|run|order|book|schedule)\b")
REPORT = re.compile(r"\b(?:there(?:'s| is| are)|we have|we've got|i see|i saw|just saw|noticed|reporting|got a|have a)\b")
HAZARD = {"spill": "spill", "leak": "spill", "coolant": "spill", "oil": "spill", "puddle": "spill",
          "shortage": "part_shortage", "out of parts": "part_shortage", "no parts": "part_shortage",
          "missing parts": "part_shortage", "ran out": "part_shortage", "overheat": "overheat", "too hot": "overheat",
          "hot": "overheat", "agv": "agv_fault", "robot cell": "robot_cell_fault", "torque": "torque_fault",
          "near miss": "near_miss", "nearly hit": "near_miss", "almost hit": "near_miss", "defect": "quality_defect",
          "scratch": "quality_defect", "dent": "quality_defect", "fault": "robot_cell_fault", "jam": "robot_cell_fault",
          "broken": "robot_cell_fault"}
LIVE = re.compile(r"\b(?:right now|now|currently|at the moment|open incidents?|what'?s (?:happening|going on|open)|"
                  r"status|anything (?:open|waiting|pending)|waiting for me|pending|on the floor today|this shift)\b")
HISTORY = re.compile(r"\b(?:why|trend|history|historic\w*|last (?:week|month|year)|since|usually|average|most|least|"
                     r"compare|keep|often|per shift|by shift|which shift)\b")
FOLLOW = re.compile(r"^(?:and|what about|how about|also|same for|same but|and for|what if|why|why not|how come|"
                    r"really|so|then|more|tell me more|go on|explain)\b")
PRONOUN = re.compile(r"\b(?:it|that|those|them|this|these|there|the same)\b")
ASKING = re.compile(r"^(?:do|does|did|how|which|what|when|where|are|is|was|were|who|why|can|could|should|has|have)\b")
IDS = re.compile(r"\b(?:[btpmfe]\d0|agv-\d)\b", re.I)


def norm(text: str) -> str:
    return " ".join(text.lower().replace("’", "'").split())


def _greeting(t: str) -> str:
    """The greeting the person used, normalised ("good morning", "hello"); the reply picks its own words from the clock."""
    m = GREETING.match(t)
    word = m.group(0).strip(" ,!.") if m else "hello"
    return " ".join(word.split()) if word.startswith("good") or word in ("morning", "afternoon", "evening") else "hello"


def part_of_day(hour: int) -> str:
    return "morning" if 5 <= hour < 12 else "afternoon" if hour < 17 and hour >= 12 else "evening" if 17 <= hour < 22 else "night"


def greet_back(said: str, hour: int | None) -> str:
    """Answer a greeting the way a colleague would: in the words that fit the actual time, without correcting anyone.
    "good morning" at 18:35 gets "Good evening"; "hello" gets "Hello"; late at night "Hello" (nobody says good night
    as a greeting)."""
    if hour is None:
        return "Hello"
    part = part_of_day(hour)
    if not any(w in said for w in ("morning", "afternoon", "evening", "day")):
        return "Hello"
    return "Hello" if part == "night" else f"Good {part}"


def _report(t: str, stations: set[str]) -> tuple[str | None, str | None]:
    kind = next((k for w, k in HAZARD.items() if re.search(rf"\b{w}", t)), None)
    station = next((s.upper() for s in re.findall(r"\b[btpmfe]\d0\b", t) if not stations or s.upper() in stations), None)
    return kind, station


def rules(text: str, history: list[dict], stations: set[str]) -> Intake | None:
    """The cases code can settle on its own; None when it is not sure (the model decides, or 'unclear')."""
    t = norm(text)
    if not t.strip(" ?!.,"):
        return Intake(intent="unclear", options=SUGGESTED[:3])
    if EMERGENCY.search(t) and not HISTORY.search(t):  # always code: people first, whatever a model would say
        kind, station = _report(t, stations)
        return Intake(intent="emergency", kind=kind or "near_miss", station=station)
    if WRITE.search(t):
        return Intake(intent="write")
    rest = FILLER.sub(" ", GREETING.sub("", t)).strip(" ,!.?")
    if GREETING.match(t) and not rest:
        return Intake(intent="greeting", reply=_greeting(t))
    if THANKS.match(t) and len(t.split()) <= 5 and not DOMAIN.search(t):
        return Intake(intent="thanks")
    if HELP.search(t) and len(t.split()) <= 10:
        return Intake(intent="help", options=SUGGESTED[:3])
    if GREETING.match(t):  # "good morning, what's open?": answer the rest, greet as well
        inner = rules(GREETING.sub("", t).strip(" ,!."), history, stations)
        if inner is not None:
            return inner.model_copy(update={"reply": inner.reply or _greeting(t)})
        return None
    if ACTION.match(t):
        kind, station = _report(t, stations)
        kind = None if kind == "agv_fault" and not re.search(r"\b(?:fault|broke\w*|stuck|dead)\b", t) else kind  # "send AGV-3"
        return Intake(intent="action", kind=kind, station=station)
    if REPORT.search(t) and not ASKING.match(t) and not HISTORY.search(t):  # "do we have many spills?" is a question
        kind, station = _report(t, stations)
        if kind:
            return Intake(intent="report", kind=kind, station=station)
    last = history[-1]["question"] if history else None
    short = len(t.split()) <= 7
    own = DOMAIN.search(t) is not None  # "why does F10 keep stopping?" stands alone even right after another question
    follows = (FOLLOW.match(t) and (len(t.split()) <= 3 or not own or t.startswith(("and", "what about", "how about",
                                                                                     "same", "also"))))
    if last and short and (follows or (PRONOUN.search(t) and not IDS.search(t) and (not own or len(t.split()) <= 4))):
        new_ids = [i.upper() for i in IDS.findall(t)]
        old_ids = IDS.findall(last)
        if new_ids and old_ids:  # "and F20?" after "Why does F10 keep stopping?"
            q = IDS.sub(lambda m, it=iter(new_ids + new_ids[-1:] * 9): next(it), last)
        else:
            q = f"{last} Follow-up: {text.strip()}"
        return Intake(intent="follow_up", question=q[:400])
    if LIVE.search(t) and not HISTORY.search(t):
        return Intake(intent="live_status")
    if DOMAIN.search(t):
        return Intake(intent="floor_question", question=text.strip())
    return None


def understand(text: str, history: list[dict], stations: set[str], kinds: set[str], ask_model=None,
               live_line: str = "") -> tuple[Intake, str]:
    """(intake, how): how is 'rules', 'nemotron' or 'rules (model unavailable)'."""
    r = rules(text, history, stations)
    if r is not None:
        return r, "rules"
    if ask_model is not None:
        try:
            m = ask_model([
                {"role": "system", "content": "You are the coordinator of 'Ask the floor', a read-only question desk for a "
                 "car final-assembly hall (stations like F10, F30, T10; AGVs AGV-1..8; incidents, shifts A/B/C, workers "
                 "and their certificates, machines, safety rules, and the agents' own decisions). Classify the message. "
                 "floor_question: answerable from the plant data or the agents' history; put the standalone question in "
                 "'question'. follow_up: only makes sense with the recent conversation; rewrite it as a standalone "
                 "question in 'question'. live_status: about what is open or happening right now. report: someone "
                 "describes a problem on the floor now (give kind and station if clear). action: asks you to do "
                 "something on the floor (you cannot; the orchestrator does, with a human gate). write: asks to change "
                 "data (refused). greeting, thanks, help. off_topic: not about the floor; reply politely in one "
                 "sentence and offer floor questions. unclear: too vague; reply with one short clarifying question and "
                 "give 2-4 options. Keep 'reply' to one or two plain sentences.\n"
                 f"Known stations: {', '.join(sorted(stations))}. Incident kinds: {', '.join(sorted(kinds))}.\n"
                 + (f"The floor now: {live_line}\n" if live_line else "")},
                {"role": "user", "content": "Recent conversation (oldest first): "
                 + ("; ".join(f"Q: {h['question']} A: {str(h.get('summary', ''))[:160]}" for h in history[-3:]) or "none")
                 + f"\nMessage: {text}"}])
            station = m.station.upper() if m.station and (m.station.upper() in stations or not stations) else None
            kind = m.kind if m.kind in kinds else None
            if m.intent in INVESTIGATE and len(m.question.strip()) < 5:
                m = m.model_copy(update={"question": text.strip()})
            return m.model_copy(update={"station": station, "kind": kind}), "nemotron"
        except Exception:  # noqa: BLE001 - the rules' answer below stands without a model
            pass
    return Intake(intent="unclear", options=SUGGESTED[:3]), "rules (model unavailable)"


# ---------------------------------------------------------------- answers that need no investigation

def _live_rows(live: dict) -> list[list]:
    rows = [["floor", "time", live.get("time")], ["floor", "shift", live.get("shift")]]
    for o in live.get("open", []):
        rows.append([o["incident"], f"{o['kind'].replace('_', ' ')} at {o['station']}", o["state"]])
    agvs = live.get("agvs", {})
    if agvs:
        rows.append(["AGVs", "available", f"{agvs.get('available', 0)} of {agvs.get('total', 0)}"])
        for a in agvs.get("problems", []):
            rows.append([a["agv"], "status", a["status"]])
    if live.get("autopilot"):
        rows.append(["autopilot", "shift", "running"])
    return rows


def live_evidence(live: dict, n: int = 1) -> dict:
    rows = _live_rows(live)
    return {"id": f"E{n}", "type": "floor", "source": "orchestrator", "ok": True,
            "task": "Live floor: the orchestrator's open incidents and the fleet now",
            "columns": ["entity", "fact", "value"], "preview": rows, "rows": len(rows)}


SHIFT_NAMES = {"A": "the early shift (A)", "B": "the late shift (B)", "N": "the night shift"}
STATUS_WORDS = {"fault": "has a fault", "stalled": "is stalled", "held": "is held for safety"}


def live_lines(live: dict) -> list[str]:
    out = []
    opened = live.get("open", [])
    waiting = [o for o in opened if o["state"].startswith("waiting")]
    if opened:
        out.append(f"{len(opened)} incident{'s' if len(opened) != 1 else ''} open: "
                   + "; ".join(f"{o['incident']} {o['kind'].replace('_', ' ')} at {o['station']} ({o['state']})"
                               for o in opened[:5]) + (" and more" if len(opened) > 5 else "") + ".")
    else:
        out.append("No incidents are open.")
    if not waiting:
        out.append("Nothing is waiting for you.")
    elif len(waiting) < len(opened) or len(opened) > 5:  # otherwise the line above already says so
        out.append(f"Waiting for your decision on the Live floor: {', '.join(o['incident'] for o in waiting[:6])}.")
    agvs = live.get("agvs") or {}
    if agvs:
        bad = agvs.get("problems", [])
        out.append(f"{agvs.get('available', 0)} of {agvs.get('total', 0)} AGVs are available"
                   + (": " + ", ".join(f"{a['agv']} {STATUS_WORDS.get(a['status'], 'is ' + a['status'])}" for a in bad[:4])
                      if bad else "") + ".")
    if live.get("autopilot"):
        out.append("The autopilot shift is running.")
    return out


def respond(intake: Intake, text: str, live: dict | None, now: datetime | None = None) -> dict:
    """summary, findings (citing the live evidence), suggestions and a hand-off for the non-investigation intents."""
    live = live or {}
    lines = live_lines(live) if live else []
    ev = [live_evidence(live)] if live else []
    cite = [{"claim": line, "evidence": ["E1"]} for line in lines] if ev else []
    when = (f"It's {live['time']}, {SHIFT_NAMES.get(live.get('shift'), 'shift ' + str(live.get('shift')))}"
            + (f"; the simulated floor runs shift {live['floor_shift']}'s crew" if live.get("floor_shift") and
               live.get("floor_shift") != live.get("shift") else "") + "." if live.get("time") else "")
    same = next((o for o in live.get("open", []) if intake.station and o["station"] == intake.station
                 and (not intake.kind or o["kind"] == intake.kind)), None)
    out = {"summary": "", "findings": [], "evidence": [], "recommendation": "", "suggestions": [], "handoff": None}
    i = intake.intent
    if i == "greeting":
        hello = greet_back(intake.reply or "hello", live.get("hour"))
        out.update(summary=f"{hello}. {when} Here is the floor right now.".replace("  ", " "),
                   findings=cite, evidence=ev, suggestions=SUGGESTED[:3])
    elif i == "thanks":
        out.update(summary="You're welcome. Ask again any time.")
    elif i == "help":
        out.update(summary="I answer questions about the assembly hall from its data: incidents and downtime, stations, "
                   "AGVs, people and certificates, safety rules, and what the agents decided and learned. A coordinator "
                   "splits your question, SQL agents query Zetaris read-only, and a critic drops any claim the rows don't "
                   "back. I can't move robots or people: report a problem on the Live floor and the orchestrator plans "
                   "the response, with you approving anything risky.", suggestions=SUGGESTED)
    elif i == "live_status":
        out.update(summary=f"{when} This is the live floor, from the orchestrator.".strip(), findings=cite, evidence=ev,
                   suggestions=["Why does F10 keep stopping?", "Which AGV is least reliable and when?"])
    elif i == "report" or i == "emergency":
        what = f"{(intake.kind or 'an incident').replace('_', ' ')}{' at ' + intake.station if intake.station else ''}"
        first = ("If anyone is hurt, or there is fire or smoke, follow the site emergency procedure first: raise the "
                 "alarm, call first aid or the emergency number, and keep people away. The agents do not replace that. "
                 if i == "emergency" else "")
        if same:
            out.update(summary=f"{first}That is already open as {same['incident']} ({same['state']}), so I won't open a "
                       "second one. Open it on the Live floor to follow or judge it.",
                       handoff={"incident": same["incident"]}, findings=cite, evidence=ev)
        else:
            out.update(summary=f"{first}That sounds like {what}. Ask the floor only reads data, so I can't dispatch "
                       "anyone from here. Report it on the Live floor: the orchestrator plans the response, code checks "
                       "safety, and you approve anything risky."
                       + ("" if intake.station else " Pick the station there, since your message didn't name one."),
                       handoff={"kind": intake.kind, "station": intake.station, "note": text.strip()[:300]})
    elif i == "action":
        waiting = [o for o in live.get("open", []) if o["state"].startswith("waiting")]
        out.update(summary="I can't do that from here: Ask the floor only reads data. The orchestrator acts on the Live "
                   "floor, where code checks every plan for safety and you approve anything risky."
                   + (f" {len(waiting)} incident{'s are' if len(waiting) != 1 else ' is'} waiting for your decision there."
                      if waiting else ""),
                   handoff={"kind": intake.kind, "station": intake.station, "note": text.strip()[:300]}
                   if intake.kind else {"view": "live"})
    elif i == "write":
        out.update(summary="I can't change data. The agents read Zetaris through a read-only login, and a SQL guard "
                   "lets through only a single SELECT on the plant tables. Changes go through the operator, not the "
                   "agents.", suggestions=SUGGESTED[:2])
    elif i == "off_topic":
        out.update(summary=intake.reply or "That's outside what I know: I only know this assembly hall.",
                   suggestions=SUGGESTED[:3])
    else:  # unclear
        out.update(summary=intake.reply or "I'm not sure what you'd like to know about the floor. Could you say a bit "
                   "more, or pick one of these?", suggestions=intake.options or SUGGESTED[:3])
    return out
