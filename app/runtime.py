"""Process-wide guarded dependencies: circuit breakers around Nightshift (app.llm) and Zetaris."""

from __future__ import annotations

from app import llm, models, zetaris
from app.cache import cached_sql, stats as cache_stats
from app.agents.health import CircuitBreaker, check_health

BREAKERS = {"nightshift": CircuitBreaker("nightshift", threshold=2, cooldown_s=60),
            "zetaris": CircuitBreaker("zetaris", threshold=3, cooldown_s=30)}

# every model call goes through the role registry (config/models.json): per-model breakers and fallback chains
# inside, and one breaker per role outside (that role's whole chain failing means safe mode for that role). It used
# to be one breaker for every role: two slow planner calls during an autopilot shift then cut off the router, the
# Ask coordinator and the Lab as well, and they all fell back to rules. BREAKERS["nightshift"] guards the probe.
ROLE_BREAKERS: dict[str, CircuitBreaker] = {}


def _per_role(fn):
    def call(lane, messages, *args, role=None, **kwargs):
        key = models.role_for(lane, role)
        breaker = ROLE_BREAKERS.get(key)
        if breaker is None:  # named "nightshift" so a failure still surfaces as LLMUnavailable (safe mode)
            breaker = ROLE_BREAKERS.setdefault(key, CircuitBreaker("nightshift", threshold=2, cooldown_s=60))
        return breaker.call(fn, lane, messages, *args, role=role, **kwargs)
    return call


chat_json = _per_role(models.chat_json)
chat = _per_role(models.chat)
# read-only results are cached for 120 s (app/cache.py); a write clears the cache
run_sql = cached_sql(BREAKERS["zetaris"].wrap(zetaris.run_sql))  # agent reads: the read-only login when set
# the operator login, for the few writes we make on purpose (the Lab's experiment row)
run_write = cached_sql(BREAKERS["zetaris"].wrap(lambda sql, *a, **k: zetaris.run_sql(sql, *a, write=True, **k)),
                       name="zetaris_write")

PROBES = {"zetaris": lambda: zetaris.run_sql("SELECT 1"), "nightshift": llm.list_models}


def health(sim=None) -> dict:
    out = check_health(BREAKERS, PROBES, sim)
    out["models"] = {m: b.status() for m, b in models.BREAKERS.items()}  # per-model breakers
    out["roles"] = {r: b.status() for r, b in sorted(ROLE_BREAKERS.items())}  # per-role breakers around them
    out["caches"] = cache_stats()
    out["zetaris_reader"] = zetaris.reader()  # agents on the read-only role, or still on the operator login
    out["model_budget"] = models.BUDGET.status()  # hourly model-call budget (thundering-herd guard)
    out["model_pins"] = models.pin_report()  # self-check at startup + live drift (answering != pinned)
    return out
