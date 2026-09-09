from unittest.mock import patch

import pytest

from app.gns3_state import NoLiveProject, list_live_nodes


def test_raises_when_no_project_resolved(app_state):
    app_state.project_id = None
    app_state.startup_warning = "no project deployed"

    class FakeApp:
        state = app_state

    with pytest.raises(NoLiveProject, match="no project deployed"):
        list_live_nodes(FakeApp())


def test_returns_live_nodes_when_project_resolved(app_state):
    app_state.project_id = "proj-1"

    class FakeApp:
        state = app_state

    fake_nodes = [{"name": "R1", "status": "started"}]
    with patch(
        "app.gns3_state.GNS3Connection.list_project_nodes", return_value=fake_nodes
    ):
        assert list_live_nodes(FakeApp()) == fake_nodes
