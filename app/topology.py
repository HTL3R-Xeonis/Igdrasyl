"""
Loads the topology YAML a `topologybuilder deploy` already used into a
Graph, the same way TopologyBuilder's own CLI does (see its
src/cli.py's visualize/deploy commands) - TopologyOperator doesn't
re-parse or re-validate the file itself, it reuses TopologyBuilder's own
validator so the two projects can never disagree on what's a valid
topology file.
"""

from __future__ import annotations

from pathlib import Path

# Import order matters here: src.graph.environment and src.connections
# have a circular import between them in TopologyBuilder itself, which
# only resolves cleanly if something under src.connections finishes
# importing first in a given process (the order TopologyBuilder's own
# src/cli.py happens to use) - importing src.graph first, on its own,
# raises ImportError. Pulling in ESXiConnection first (unused otherwise
# here) keeps this module safe to import standalone.
from src.connections.esxi_connection import ESXiConnection as _  # noqa: F401
from src.graph import Graph
from src.settings import Settings
from src.topology_file_validation import TopologyFileValidation


def load_graph() -> Graph:
    """
    Validates and parses Settings.TOPOLOGY_FILE into a Graph.
    :raises FileNotFoundError: if the topology file doesn't exist.
    :raises ValueError: if the topology file is invalid.
    """
    if not Settings.TOPOLOGY_FILE:
        raise ValueError(
            "No project selected. Choose Project on the host tile to select a topology."
        )
    validator = TopologyFileValidation(Settings.TOPOLOGY_FILE)
    validator.validate_file()
    return Graph(validator.nodes, validator.edges)


def resolve_project_name() -> str | None:
    """
    The GNS3 project name a deploy of Settings.TOPOLOGY_FILE would have
    used: Settings.GNS3.PROJECT_NAME if set, otherwise the topology
    file's own stem - mirrors TopologyBuilder's own (private)
    src.cli._resolve_project_name rather than importing it directly.
    """
    if Settings.GNS3.PROJECT_NAME is not None:
        return Settings.GNS3.PROJECT_NAME
    return Path(Settings.TOPOLOGY_FILE).stem if Settings.TOPOLOGY_FILE else None
