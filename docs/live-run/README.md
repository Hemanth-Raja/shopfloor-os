# A real live-mode trace

Recorded live on 26 Oct 2026 (00:25 UTC), not in sample mode: incident `SIM-42-008`, a part shortage at F30, read from Zetaris (one federated query over `sf_fleet`, `sf_hr`, `sf_plant`, `sf_ops`) with every model call through Meterless Nightshift. Nemotron 3.5 Lightning 30B classified the alarm and routed two hops; Nemotron 3 Ultra 550B wrote a v2 plan, which the safety critic rejected (`needs_revision`, R-CROSSING), so the v1 plan was kept, gated and executed.

- [`trace-SIM-42-008.jsonl`](trace-SIM-42-008.jsonl): the full agent trace, one line per step, copied unchanged from `logs/`.
- [`llm-SIM-42-008.jsonl`](llm-SIM-42-008.jsonl): the four Nightshift calls for that run (`logs/llm.jsonl`), each with requested and answering model, latency, tokens and its Ed25519-verified receipt id. Prompt bodies are not included, and neither file holds a key, token or address.
