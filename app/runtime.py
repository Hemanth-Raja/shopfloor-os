"""Process-wide guarded dependencies: circuit breakers around Nightshift (app.llm) and Zetaris."""

from __future__ import annotations

from app import llm, zetaris
from app.agents.health import CircuitBreaker, check_health

BREAKERS = {"nightshift": CircuitBreaker("nightshift", threshold=2, cooldown_s=60),
            "zetaris": CircuitBreaker("zetaris", threshold=3, cooldown_s=30)}

chat_json = BREAKERS["nightshift"].wrap(llm.chat_json)
chat = BREAKERS["nightshift"].wrap(llm.chat)
run_sql = BREAKERS["zetaris"].wrap(zetaris.run_sql)

PROBES = {"zetaris": lambda: zetaris.run_sql("SELECT 1"), "nightshift": llm.list_models}


def health(sim=None) -> dict:
    return check_health(BREAKERS, PROBES, sim)
