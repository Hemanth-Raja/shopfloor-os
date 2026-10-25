"""VDA 5050: AGV dispatches go out as standard orders; AGVs report standard states."""
from types import SimpleNamespace

from app import vda5050
from app.sim.grid import CELL_M


class FakeGrid:
    def __init__(self, crossing=()):
        self.crossing = set(crossing)

    def is_crossing(self, x, y):
        return (x, y) in self.crossing

    def nearest_walkable(self, x, y):
        return (x, y)


def test_an_order_has_corner_nodes_metres_and_the_crossing_speed():
    path = [(0, 0), (1, 0), (2, 0), (2, 1), (2, 2), (2, 2), (3, 2), (4, 2)]  # a turn, a crossing at (3, 2)
    msg = vda5050.order("agv:plant:AGV-3", path, 1.5, 0.5, order_id="SIM-42-001", update_id=0, header_id=7,
                        grid=FakeGrid({(3, 2)}), pickup=(1, 0))
    assert msg["version"] == "2.0.0" and msg["serialNumber"] == "AGV-3" and msg["orderId"] == "SIM-42-001"
    ids = [n["nodeId"] for n in msg["nodes"]]
    assert ids[0] == "c0_0" and ids[-1] == "c4_2" and "c2_0" in ids and "c1_0" in ids  # the turn and the pickup
    assert [n["sequenceId"] for n in msg["nodes"]] == list(range(0, 2 * len(ids), 2))
    assert [e["sequenceId"] for e in msg["edges"]] == list(range(1, 2 * len(ids) - 1, 2))
    assert msg["nodes"][-1]["nodePosition"] == {"x": 4 * CELL_M, "y": 2 * CELL_M, "mapId": vda5050.MAP_ID}
    assert {e["maxSpeed"] for e in msg["edges"] if "3_2" in e["edgeId"]} == {0.5}
    assert any(a["actionType"] == "pick" for n in msg["nodes"] for a in n["actions"])
    assert msg["nodes"][-1]["actions"][-1]["actionType"] == "drop"


def test_the_fleet_sends_orders_for_agvs_only_and_states_report_faults(tmp_path):
    agv = SimpleNamespace(id="agv:plant:AGV-1", kind="agv", pos=(3, 4), status="fault")
    worker = SimpleNamespace(id="worker:plant:W-01", kind="worker", pos=(1, 1), status="idle")
    sim = SimpleNamespace(agents={agv.id: agv, worker.id: worker}, grid=FakeGrid())
    inner = SimpleNamespace(dispatch=lambda *a, **k: [(0, 0), (1, 0)])
    bridge = vda5050.Bridge(tmp_path / "vda.jsonl")
    fleet = vda5050.Fleet(inner, bridge, sim)
    fleet.dispatch(worker.id, (1, 0))
    fleet.dispatch(agv.id, (1, 0), 1.0, None, via=(0, 0), key="SIM-42-002:agv:plant:AGV-1")
    assert len(bridge.orders) == 1 and bridge.orders[0]["orderId"] == "SIM-42-002"
    assert "uagv/v2/ShopFloorOS-Sim/AGV-1/order" in (tmp_path / "vda.jsonl").read_text()
    [st] = bridge.states(sim)
    assert st["errors"][0]["errorLevel"] == "FATAL" and st["orderId"] == "SIM-42-002" and not st["driving"]
    assert st["agvPosition"]["x"] == 3 * CELL_M
