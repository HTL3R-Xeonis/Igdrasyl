"""
Add Link in the design canvas has no deployed node to ask for its ports. The picker therefore offers a list computed
from what is known (GNS3 template port formats, the fixed names of GNS3's built-in nodes, the adapter names of an ESXi VM)
and keeps a free-text escape; it never shows an empty text field for a node whose ports are knowable.
"""

from frontend_js import read_app_js


def _prompt_function() -> str:
    js = read_app_js()
    start = js.index("async function promptForInterface(")
    end = js.index("async function handleLinkModeClick(")
    return js[start:end]


def test_an_esxi_vm_is_offered_its_own_interfaces_and_one_new_adapter():
    js = read_app_js()
    assert "const ESXI_MAX_ADAPTERS = 10;" in js
    assert "function nextEsxiAdapterName(existing)" in js
    body = _prompt_function()
    assert "computedInterfaceChoices(" in body and "(new adapter)" in body
    helper = js[
        js.index("function computedInterfaceChoices(") : js.index(
            "async function promptForInterface("
        )
    ]
    assert "templates.esxi" in helper and "nextEsxiAdapterName(names)" in helper
    # no list of ten invented names any more
    assert "esxiAdapterNames" not in js


def test_a_computed_list_keeps_an_other_name_escape():
    body = _prompt_function()
    assert 'label: "Other name…"' in body
    assert "CUSTOM_INTERFACE" in body
    # choosing it leads to the text prompt, it is never returned as an interface name
    assert body.index("chosen === CUSTOM_INTERFACE") < body.index("return chosen;")


def test_the_free_text_fallback_names_a_failed_template_service():
    body = _prompt_function()
    assert "templates.warnings" in body
    assert "the template service did not answer" in body


def test_template_warnings_reach_the_screen():
    js = read_app_js()
    assert "availableTemplates.warnings?.length" in js
    assert "setMenubarMessage(availableTemplates.warnings.join" in js


def test_only_a_missing_live_port_list_falls_back_to_the_computed_one():
    body = _prompt_function()
    # 404 (an ESXi VM, a design node) is the expected case; any other failure is shown, not papered over
    assert "resp.status !== 404" in body
    assert "Could not read the ports of" in body
    assert (
        body.count("Could not read the ports of") == 2
    )  # a failed status and a failed request


# --- nodes without an instance are named before a start runs into them ---------------------------------------------------


def test_a_bulk_action_names_the_nodes_without_an_instance_first():
    js = read_app_js()
    assert "function nodesWithoutInstance(names)" in js
    start = js.index("async function lifecycleMany(")
    end = js.index("async function confirmAndRunMany(")
    body = js[start:end]
    # the check comes before the first request is sent
    assert body.index("nodesWithoutInstance(names)") < body.index("fetch(`/nodes/")
    assert "describeMissingInstances(missing)" in body


def test_the_canvas_announces_missing_instances_once_per_set_of_nodes():
    js = read_app_js()
    assert "function announceMissingInstances(" in js
    assert "announceMissingInstances(nodesResult, health)" in js
    assert "lastMissingInstancesNotice" in js


def test_a_failed_template_load_is_not_cached_and_shows_the_servers_reason():
    js = read_app_js()
    assert "availableTemplates && !availableTemplates._error" in js
    assert "(HTTP ${resp.status})" in js


def test_a_failed_layout_read_is_reported():
    js = read_app_js()
    assert "Could not read the saved node positions" in js


def test_a_refused_host_activation_is_shown_to_the_user():
    from pathlib import Path

    projects = (
        Path(__file__).resolve().parent.parent / "app" / "static" / "projects.js"
    ).read_text(encoding="utf-8")
    assert "if (!(error instanceof TypeError)) throw error;" in projects


# --- Stage Live Edits: choose, do not type ----------------------------------------------------------------------------


def _staging_html() -> str:
    from pathlib import Path

    return (
        Path(__file__).resolve().parent.parent / "app" / "static" / "index.html"
    ).read_text(encoding="utf-8")


def test_the_staging_node_form_offers_roles_and_images_to_choose_from():
    html = _staging_html()
    assert '<select id="staging-node-role"' in html
    assert '<select id="staging-node-image"' in html
    for role in ("PC", "VM", "ROUTER", "SWITCH", "FW"):
        assert f'<option value="{role}">{role}</option>' in html
    # no free-text role or image field any more
    assert 'input id="staging-node-role"' not in html
    assert 'input id="staging-node-image"' not in html


def test_the_staging_images_follow_the_role_with_the_palettes_own_split():
    js = read_app_js()
    body = js[
        js.index("async function fillStagingImages") : js.index(
            "stagingRoleSelect.addEventListener"
        )
    ]
    assert 'classifyTemplateRole(name, group) || "PC"' in body
    # a failed template load is named, not answered with a field that cannot work
    assert "templates._error" in body and "Could not load the image templates" in body


def test_the_staging_link_form_chooses_its_nodes_and_suggests_interfaces():
    html = _staging_html()
    assert (
        '<select id="staging-link-node1"' in html
        and '<select id="staging-link-node2"' in html
    )
    assert 'list="staging-if1-options"' in html and "datalist" in html
    js = read_app_js()
    assert "computedInterfaceChoices(node.image, known, templates)" in js


def test_a_live_esxi_vm_list_still_offers_one_new_adapter():
    body = _prompt_function()
    live = body[: body.index("} else {")]
    assert "nextEsxiAdapterName(ports.map((p) => p.name))" in live
    assert "(new adapter)" in live and "CUSTOM_INTERFACE" in live
