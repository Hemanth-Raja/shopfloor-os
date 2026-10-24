"""Build the static replay page: docs/site/index.html, one self-contained file (no server, no network).

It embeds the recorded runs in output_examples/ (and, when present, the step details of their last run in
logs/trace-example-N.jsonl) plus the headline numbers of docs/eval.md. Open the file in a browser; nothing is
published. Rebuild: .venv/bin/python scripts/build_site.py
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "site" / "index.html"
KEEP = ("monitor", "candidates", "planner", "critic", "dryrun", "refine", "restraint", "gate", "verifier", "reflex",
        "executor")


def last_run(path: Path) -> list[dict]:
    if not path.exists():
        return []
    lines = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    start = max((i for i, s in enumerate(lines) if s.get("agent") == "reflex"), default=0)
    steps = []
    for s in lines[start:]:
        d = s.get("detail", {})
        slim = {k: d[k] for k in ("attempt", "verdict", "p_real", "summary", "assignees", "agv_routes", "decision",
                                  "reasons", "gate", "collisions", "eta_s", "model", "kept", "swapped", "why")
                if k in d}
        if s.get("agent") == "critic":
            slim["violations"] = [{"rule": v["rule_id"], "detail": v["detail"],
                                   "clauses": [c["ref"] for c in v.get("citations", [])]} for v in d.get("violations", [])]
        steps.append({"agent": s.get("agent"), "ok": s.get("ok"), "ms": s.get("ms"), "detail": slim})
    return steps


def runs() -> list[dict]:
    out = []
    for f in sorted(ROOT.glob("output_examples/example_*.json"), key=lambda p: int(re.findall(r"\d+", p.stem)[0])):
        n = re.findall(r"\d+", f.stem)[0]
        d = json.loads(f.read_text(encoding="utf-8"))
        inp = ROOT / "input_examples" / f"example_{n}.json"
        o = d["output"]
        out.append({"id": d["run_id"], "input": json.loads(inp.read_text())["input"] if inp.exists() else {},
                    "summary": o.get("summary"), "incident": o.get("incident"), "decision": o.get("decision"),
                    "reasons": o.get("reasons", []), "plan": o.get("plan"), "citations": o.get("citations", []),
                    "gate": o.get("gate"), "gate_reasons": o.get("gate_reasons", []),
                    "critic_rejections": o.get("critic_rejections"), "path": o.get("path", []),
                    "models": d.get("models", []), "seconds": d.get("execution_time_seconds"),
                    "steps": last_run(ROOT / "logs" / f"trace-{d['run_id']}.jsonl")})
    return out


def eval_lines() -> list[str]:
    p = ROOT / "docs" / "eval.md"
    if not p.exists():
        return []
    return [re.sub(r"\*\*|`", "", ln[2:]) for ln in p.read_text(encoding="utf-8").splitlines() if ln.startswith("- ")][:10]


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Shop-Floor OS Replay</title>
<style>
:root{--bg:#f7f7f5;--panel:#fff;--ink:#1d1d1b;--muted:#6b6b66;--line:#e2e2dc;--ok:#2f7d4f;--bad:#b3412f;--accent:#2f5d9e;--chip:#eef1f6}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#151514;--panel:#1f1f1d;--ink:#ecece6;--muted:#a3a39b;--line:#33332f;--ok:#6cc38f;--bad:#e2806f;--accent:#8fb1e8;--chip:#26303f}}
:root[data-theme=dark]{--bg:#151514;--panel:#1f1f1d;--ink:#ecece6;--muted:#a3a39b;--line:#33332f;--ok:#6cc38f;--bad:#e2806f;--accent:#8fb1e8;--chip:#26303f}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:1040px;margin:0 auto;padding:24px 16px 64px}h1{font-size:24px;margin:0 0 4px}h2{font-size:16px;margin:24px 0 8px}
.sub{color:var(--muted);margin:0 0 20px}.tabs{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:16px}
.tabs button{border:1px solid var(--line);background:var(--panel);color:var(--ink);border-radius:8px;padding:6px 10px;cursor:pointer;font:inherit}
.tabs button[aria-pressed=true]{border-color:var(--accent);color:var(--accent);font-weight:600}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin-bottom:12px}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.chip{background:var(--chip);border-radius:6px;padding:2px 8px;font-size:13px}
.ok{color:var(--ok)}.bad{color:var(--bad)}.muted{color:var(--muted)}
ol.steps{list-style:none;padding:0;margin:0}ol.steps li{border-left:3px solid var(--line);padding:6px 0 6px 12px;margin-left:6px;opacity:.35;transition:opacity .2s}
ol.steps li.on{opacity:1;border-color:var(--accent)}ol.steps li.fail{border-color:var(--bad)}
.ctl{display:flex;gap:8px;margin:8px 0 12px}.ctl button{font:inherit;padding:6px 12px;border-radius:8px;border:1px solid var(--line);background:var(--panel);color:var(--ink);cursor:pointer}
code{font:13px ui-monospace,Consolas,monospace;word-break:break-word}ul{margin:4px 0;padding-left:20px}
</style></head><body><main>
<h1>Shop-Floor OS: replay</h1>
<p class="sub">Recorded agent runs from the repository, replayed in the browser. AI agents plan, robots execute, humans judge. Nothing here calls a model.</p>
<div class="tabs" id="tabs"></div><div id="run"></div>
<h2>Evaluation (docs/eval.md)</h2><div class="card" id="eval"></div>
</main><script>
const RUNS=__RUNS__, EVAL=__EVAL__;
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
let cur=0,timer=null,shown=0;
function tabs(){document.getElementById("tabs").innerHTML=RUNS.map((r,i)=>`<button aria-pressed="${i===cur}" onclick="pick(${i})">${esc((r.incident||{}).kind||r.id).replace(/_/g," ")} <span class="muted">${esc((r.incident||{}).station_id||"")}</span></button>`).join("")}
function stepLine(s){const d=s.detail||{};let x="";
 if(s.agent==="critic"&&d.violations&&d.violations.length)x=d.violations.map(v=>`<div class="bad">${esc(v.rule)}: ${esc(v.detail)} <span class="muted">${esc(v.clauses.join(", "))}</span></div>`).join("");
 else if(d.summary)x=esc(d.summary);else if(d.assignees)x="assignees "+esc(d.assignees.map(a=>a.split(":").pop()).join(", "))+(d.model?` <span class="muted">(${esc(d.model)})</span>`:"");
 else if(d.decision)x=esc(d.decision)+(d.reasons?` <span class="muted">${esc(d.reasons.join("; "))}</span>`:"");
 else if(d.gate)x=esc(d.gate);
 return `<b>${esc(s.agent)}</b> <span class="${s.ok?"ok":"bad"}">${s.ok?"ok":"rejected"}</span> <span class="muted">${s.ms??""} ms</span><div>${x}</div>`}
function render(){const r=RUNS[cur],inc=r.incident||{},p=r.plan||{};
 const steps=r.steps.length?r.steps:r.path.map(h=>({agent:h.agent,ok:true,ms:"",detail:{summary:`${h.by}: ${h.reason}`}}));
 document.getElementById("run").innerHTML=`<div class="card"><div class="row"><b>${esc(inc.kind)}</b><span class="chip">${esc(inc.station_id||inc.zone_id)}</span><span class="chip">severity ${esc(inc.severity)}</span><span class="chip">decision ${esc(r.decision)}</span><span class="chip">gate ${esc(r.gate)}</span><span class="chip">critic rejections ${esc(r.critic_rejections)}</span></div>
 <p>${esc(r.summary)}</p><div class="muted">sensors <code>${esc(JSON.stringify(inc.sensors))}</code></div></div>
 <div class="card"><b>Agent steps</b><div class="ctl"><button onclick="play()">Play</button><button onclick="all_()">Show all</button></div>
 <ol class="steps">${steps.map((s,i)=>`<li id="s${i}" class="${s.ok?"":"fail"}">${stepLine(s)}</li>`).join("")}</ol></div>
 <div class="card"><b>Plan</b><ul><li>assignees: ${esc((p.assignee_ids||[]).join(", "))}</li>${(p.agv_routes||[]).map(a=>`<li>${esc(a.agv_id)} to ${esc(a.to_station)} at ${a.speed_mps} m/s, crossings ${a.crossing_speed_mps} m/s</li>`).join("")}<li>${esc(p.rationale)}</li></ul>
 <b>Why</b><ul>${r.reasons.concat(r.gate_reasons).map(x=>`<li>${esc(x)}</li>`).join("")}</ul>
 <b>Clauses</b><ul>${r.citations.map(c=>`<li><b>${esc(c.ref)}</b> ${esc(c.title)}: <span class="muted">${esc(c.text)}</span></li>`).join("")}</ul>
 <div class="muted">models: ${esc(r.models.join(", "))}</div></div>`;
 shown=0;clearInterval(timer);steps.forEach((_,i)=>{});play()}
function play(){clearInterval(timer);shown=0;document.querySelectorAll("ol.steps li").forEach(li=>li.classList.remove("on"));
 timer=setInterval(()=>{const li=document.getElementById("s"+shown);if(!li){clearInterval(timer);return}li.classList.add("on");shown++},450)}
function all_(){clearInterval(timer);document.querySelectorAll("ol.steps li").forEach(li=>li.classList.add("on"))}
function pick(i){cur=i;tabs();render()}
document.getElementById("eval").innerHTML=EVAL.length?"<ul>"+EVAL.map(x=>`<li>${esc(x)}</li>`).join("")+"</ul>":"<span class=muted>Run python run.py eval</span>";
tabs();render();
</script></body></html>
"""


def build() -> Path:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(runs(), separators=(",", ":")).replace("</", "<\\/")
    OUT.write_text(PAGE.replace("__RUNS__", data).replace("__EVAL__", json.dumps(eval_lines()).replace("</", "<\\/")),
                   encoding="utf-8")
    return OUT


if __name__ == "__main__":
    print(build())
