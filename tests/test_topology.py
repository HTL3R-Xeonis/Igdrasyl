from src.graph.environment import Environment


def test_load_graph_parses_fixture_topology(graph):
    assert set(graph.nodes) == {"R1", "SW1", "VM1"}
    assert graph.nodes["R1"].env == Environment.ON_GNS3
    assert graph.nodes["SW1"].env == Environment.ON_GNS3
    assert graph.nodes["VM1"].env == Environment.ON_ESXI


def test_edges_connect_the_right_interfaces(graph):
    r1 = graph.nodes["R1"]
    sw1 = graph.nodes["SW1"]
    assert r1.interfaces["gi0/0"].neighbour is sw1
    assert sw1.interfaces["Ethernet0"].neighbour is r1
