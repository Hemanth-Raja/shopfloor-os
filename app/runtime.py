"""Process-wide guarded dependencies: circuit breakers around Nightshift (app.llm) and Zetaris."""

from __future__ import annotations

from app import llm, models, zetaris
from app.cache import cached_sql, stats as cache_stats
from app.agents.health import CircuitBreaker, check_health

BREAKERS = {"nightshift": CircuitBreaker("nightshift", threshold=2, cooldown_s=60),
            "zetaris": CircuitBreaker("zetaris", threshold=3, cooldown_s=30)}

# every model call goes through the role registry (config/models.json): per-model breakers and fallback chains
# inside, and this Nightshift-wide breaker outside (all models failing means safe mode)
chat_json = BREAKERS["nightshift"].wrap(models.chat_json)
chat = BREAKERS["nightshift"].wrap(models.chat)
# read-only results are cached for 120 s (app/cache.py); a write clears the cache
run_sql = cached_sql(BREAKERS["zetaris"].wrap(zetaris.run_sql))

PROBES = {"zetaris": lambda: zetaris.run_sql("SELECT 1"), "nightshift": llm.list_models}


def health(sim=None) -> dict:
    out = check_health(BREAKERS, PROBES, sim)
    out["models"] = {m: b.status() for m, b in models.BREAKERS.items()}  # per-model breakers
    out["caches"] = cache_stats()
    out["model_pins"] = models.pin_report()  # self-check at startup + live drift (answering != pinned)
    return out
