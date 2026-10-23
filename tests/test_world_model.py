"""Meterless World Model conformance suite, ported to pytest.

Part 1 ports engines/world-model/conformance/runner.ts check for check (ids sid-001 ... audit-001).
Part 2 ports the reference implementation's smoke tests (reference/test/smoke.test.ts).
Sources are kept in data/raw/meterless/world-model/. No network, Zetaris or Nightshift needed.
"""

from datetime import datetime, timezone

import pytest

from app.engines.world_model import (
    ProvenanceError, WorldModel, canonical_json, decide, entity_id_from_name, name_similarity, normalize_name,
)

OBSERVATIONS = [
    {"externalId": "a1", "title": "Fed holds rates", "at": "2026-05-17T14:00:00.000Z",
     "source": {"kind": "rss", "at": "2026-05-17T14:00:00.000Z"}, "entities": [{"type": "person", "name": "Jerome Powell"}]},
    {"externalId": "a2", "title": "Powell signals patience", "at": "2026-05-17T16:30:00.000Z",
     "source": {"kind": "rss", "at": "2026-05-17T16:30:00.000Z"}, "entities": [{"type": "person", "name": "Jerome Powell"}]},
]
AT = "2026-05-17T10:00:00.000Z"


def clock():
    return datetime(2026, 5, 18, tzinfo=timezone.utc)


def src(at=AT, kind="manual", **extra):
    return {"kind": kind, "at": at, **extra}


# ---------------------------------------------------------------- Part 1: conformance/runner.ts


def test_sid_001_diacritics_and_nbsp_normalize_identically():
    assert normalize_name("José  García") == normalize_name("Jose Garcia")
    assert entity_id_from_name("person", "José García") == entity_id_from_name("person", "jose garcia")


def test_sid_002_distinct_names_stay_distinct():
    assert entity_id_from_name("org", "Acme Corp") != entity_id_from_name("org", "Acme Corporation")
    assert entity_id_from_name("person", "x") != entity_id_from_name("org", "x")


def test_band_001_merge_bands_at_the_boundaries():
    assert decide(0.85) == "auto-merge"
    assert decide(0.84) == "review"
    assert decide(0.82) == "review"
    assert decide(0.81) == "new"


def test_prov_001_writes_without_provenance_are_rejected():
    world = WorldModel(namespace="conf")
    with pytest.raises(ProvenanceError):
        world.upsert_entity("person", name="No Provenance", source=None)


def test_idem_001_same_batch_twice_produces_an_empty_diff():
    world = WorldModel(namespace="conf", clock=clock)
    world.ingest(OBSERVATIONS)
    shape1 = world.state_shape_canonical()
    second = world.ingest(OBSERVATIONS)
    assert second["changed"] is False
    assert world.state_shape_canonical() == shape1


def test_idem_002_a_mutated_batch_changes_the_shape():
    world = WorldModel(namespace="conf", clock=clock)
    world.ingest(OBSERVATIONS)
    shape1 = world.state_shape_canonical()
    mutated = [{**OBSERVATIONS[0], "title": OBSERVATIONS[0]["title"] + " (edited)"}, OBSERVATIONS[1]]
    run = world.ingest(mutated)
    assert run["changed"] is True
    assert world.state_shape_canonical() != shape1


def test_bitemp_001_superseding_edge_closes_the_old_one():
    world = WorldModel(namespace="conf")
    world.upsert_entity("player", external_key=("g", "p1"), attrs={}, source=src("2087-01-01T00:00:00.000Z"))
    world.upsert_entity("team", external_key=("g", "a"), attrs={}, source=src("2087-01-01T00:00:00.000Z"))
    world.upsert_entity("team", external_key=("g", "b"), attrs={}, source=src("2088-03-01T00:00:00.000Z"))
    world.relate("player:g:p1", "plays-for", "team:g:a", valid_from="2087-01-01T00:00:00.000Z", source=src("2087-01-01T00:00:00.000Z"))
    world.relate("player:g:p1", "plays-for", "team:g:b", valid_from="2088-03-01T00:00:00.000Z", source=src("2088-03-01T00:00:00.000Z"))
    now = world.view("graph").neighbors_of("player:g:p1", via="plays-for")
    assert [e["id"] for e in now] == ["team:g:b"]
    then = world.view("graph").neighbors_of("player:g:p1", via="plays-for", at="2087-06-01T00:00:00.000Z")
    assert [e["id"] for e in then] == ["team:g:a"]


def test_audit_001_every_mutation_writes_an_audit_entry_in_order():
    world = WorldModel(namespace="conf")
    world.upsert_entity("x", name="Audit One", source=src("2026-01-01T00:00:00.000Z"))
    world.upsert_entity("x", name="Audit Two", source=src("2026-01-02T00:00:00.000Z"))
    audit = world.audit()
    assert len(audit) >= 2
    assert all(audit[i]["seq"] > audit[i - 1]["seq"] for i in range(1, len(audit)))


# ---------------------------------------------------------------- Part 2: reference smoke tests


def test_normalize_plain_form():
    assert normalize_name("Jose Garcia") == "jose garcia"
    assert normalize_name("Zero​Width﻿") == "zerowidth"


def test_decide_above_auto_merge():
    assert decide(0.86) == "auto-merge"


def test_snapshot_view_answers_point_in_time_queries():
    world = WorldModel(namespace="t")
    world.upsert_entity("player", external_key=("g", "p1"), attrs={"name": "P One", "overall": 87}, source=src("2087-08-01T12:00:00.000Z"))
    world.upsert_entity("player", external_key=("g", "p1"), attrs={"overall": 94, "tier": "all-pro"}, source=src("2088-01-20T12:00:00.000Z"))
    august = world.view("snapshot").entity_at("player:g:p1", "2087-08-15")
    feb = world.view("snapshot").entity_at("player:g:p1", "2088-02-01")
    assert august["attrs"]["overall"] == 87 and "tier" not in august["attrs"]
    assert feb["attrs"]["overall"] == 94 and feb["attrs"]["tier"] == "all-pro"


def test_fact_conflicts_highest_confidence_wins():
    world = WorldModel(namespace="t", conflict_policy="highest-confidence-wins")
    world.assert_fact("person:jerome-powell", "title", "Chair", confidence=0.95, source=src("2026-05-17T09:00:00.000Z", "rss", url="a"))
    world.assert_fact("person:jerome-powell", "title", "Chairman", confidence=0.7, source=src("2026-05-17T09:30:00.000Z", "rss", url="b"))
    assert world.view("graph").entity("person:jerome-powell")["attrs"]["title"] == "Chair"
    facts = world.view("facts").about("person:jerome-powell")
    assert len(facts) == 2
    assert len([f for f in facts if f.get("supersededBy")]) == 1


def test_fact_conflicts_most_recent_wins_by_default():
    world = WorldModel(namespace="t")
    world.assert_fact("agv:plant:AGV-3", "battery_pct", 80, source=src("2026-05-17T09:00:00.000Z"))
    world.assert_fact("agv:plant:AGV-3", "battery_pct", 41, source=src("2026-05-17T10:00:00.000Z"))
    assert world.view("graph").entity("agv:plant:AGV-3")["attrs"]["battery_pct"] == 41


def test_name_resolution_auto_merges_or_queues_by_band():
    world = WorldModel(namespace="t")
    world.upsert_entity("person", name="Jerome Powell", source=src())
    world.upsert_entity("person", name="jerome powell", source=src())
    merged = world.upsert_entity("person", name="Jerome Powel", source=src())
    assert merged["id"] == entity_id_from_name("person", "Jerome Powell")
    assert world.view("graph").entity(entity_id_from_name("person", "Jerome Powel"))["id"] == entity_id_from_name("person", "Jerome Powell")
    sim = name_similarity("Jerome Powell x", "Jerome Powell abc")
    assert 0.82 <= sim < 0.85
    world.upsert_entity("person", name="Jerome Powell abc", source=src())
    world.upsert_entity("person", name="Jerome Powell x", source=src())
    assert any(p["status"] == "pending" for p in world.merge_queue())


def test_ingest_report_counts_and_mutation():
    observations = [{**o, "externalId": f"art-{i}"} for i, o in enumerate(OBSERVATIONS, 1)]
    world = WorldModel(namespace="t", clock=lambda: datetime(2026, 5, 17, 10, tzinfo=timezone.utc))
    first = world.ingest(observations)
    assert first["processed"] == 2 and first["clusters"] > 0
    shape1 = world.state_shape_canonical()
    second = world.ingest(observations)
    assert second["processed"] == 0 and second["skippedUnchanged"] == 2 and second["changed"] is False
    assert world.state_shape_canonical() == shape1
    third = world.ingest([{**observations[0], "title": "Fed holds rates steady"}])
    assert third["changed"] is True
    assert world.state_shape_canonical() != shape1


def test_single_writer_writes_serialize_in_audit_order():
    world = WorldModel(namespace="t")
    world.upsert_entity("x", name="First Entity", source=src("2026-01-01T00:00:00.000Z"))
    world.upsert_entity("x", name="Second Entity", source=src("2026-01-02T00:00:00.000Z"))
    audit = world.audit()
    assert len(audit) == 2
    assert entity_id_from_name("x", "First Entity") in audit[0]["summary"]
    assert audit[0]["seq"] < audit[1]["seq"]


def test_custom_views_fold_over_matching_events():
    world = WorldModel(namespace="t")
    world.upsert_entity("account", external_key=("sf", "a1"), attrs={}, source=src())
    world.upsert_context("campaign", external_key=("sf", "c1"), attrs={}, source=src())
    world.relate("account:sf:a1", "open-deal", "campaign:sf:c1", attrs={"stage": "negotiation", "daysInStage": 14}, source=src())

    def reducer(state, event):
        p = event["payload"]
        if p.get("type") == "open-deal" and (p.get("attrs") or {}).get("daysInStage", 0) > 10:
            state.append({"account": p["from"], "days": p["attrs"]["daysInStage"]})
        return state

    world.register_view("stuck-deals", events=["relate"], initial_state=[], reducer=reducer)
    stuck = world.view("stuck-deals").get()
    assert len(stuck) == 1 and stuck[0]["days"] == 14


def test_is_deterministic():
    def go():
        world = WorldModel(namespace="t", clock=clock)
        world.upsert_entity("person", name="Ada Lovelace", source=src())
        world.assert_fact(entity_id_from_name("person", "Ada Lovelace"), "field", "mathematics", confidence=0.9, source=src())
        return world.state_shape_canonical() + canonical_json(world.audit())

    assert go() == go()


def test_relate_is_a_content_addressed_no_op_and_local_storage_replays(tmp_path):
    world = WorldModel(namespace="persist", storage="local", directory=tmp_path)
    world.upsert_entity("station", external_key=("plant", "F10"), name="Wheels and tyres", source=src())
    world.upsert_entity("machine", external_key=("plant", "M-06"), name="Wheel nutrunner", source=src())
    first = world.relate("machine:plant:M-06", "located-at", "station:plant:F10", source=src())
    again = world.relate("machine:plant:M-06", "located-at", "station:plant:F10", source=src())
    assert first["id"] == again["id"] and len(world.events()) == 3
    reloaded = WorldModel(namespace="persist", storage="local", directory=tmp_path)
    assert reloaded.state_shape_canonical() == world.state_shape_canonical()
