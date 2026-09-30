"""Editable, non-secret profile fields and bounded reachability checks."""

import re
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

import requests
import yaml
from src.settings import Settings

from app import settings_profiles

# Capture library defaults before lifespan loads a host's settings.
FIELDS = {
    "esxi.ip": Settings.ESXI.IP,
    "esxi.port": Settings.ESXI.PORT,
    "esxi.username": Settings.ESXI.USERNAME,
    "esxi.gns3_vm_name": Settings.ESXI.GNS3_VM_NAME,
    "gns3.port": Settings.GNS3.PORT,
    "gns3.username": Settings.GNS3.USERNAME,
    "api.esxi_template_server_url": Settings.API.ESXI_TEMPLATE_SERVER_URL,
    "api.gns3_template_server_url": Settings.API.GNS3_TEMPLATE_SERVER_URL,
    "api.topology_generator_url": Settings.API.TOPOLOGY_GENERATOR_URL,
}

# Every override accepted by TopologyBuilder's YAML loader.
FIELDS.update(
    {
        "log_file_path": Settings.LOG_FILE_PATH,
        "generate_max_retries": Settings.GENERATE_MAX_RETRIES,
        "incremental_deploy": Settings.INCREMENTAL_DEPLOY,
        **{
            f"esxi.{name}": getattr(Settings.ESXI, name.upper())
            for name in (
                "virtual_switch",
                "trunk_port_group",
                "ignore_port_groups",
                "datastore",
                "delete_unused_vms",
                "gns3_vm_ova_path",
                "gns3_vm_mgmt_network",
                "gns3_vm_mac_range",
            )
        },
        "gns3.parent_interface": Settings.GNS3.PARENT_INTERFACE,
        "api.ova_deploy_url": Settings.API.OVA_DEPLOY_URL,
    }
)
GROUPS = [
    (
        "General",
        [
            "log_file_path",
            "generate_max_retries",
            "incremental_deploy",
        ],
    ),
    (
        "ESXi host",
        [
            "esxi.ip",
            "esxi.port",
            "esxi.username",
            "esxi.datastore",
            "esxi.delete_unused_vms",
        ],
    ),
    (
        "Networking",
        [
            "esxi.virtual_switch",
            "esxi.trunk_port_group",
            "esxi.ignore_port_groups",
            "gns3.parent_interface",
        ],
    ),
    (
        "GNS3",
        [
            "esxi.gns3_vm_name",
            "gns3.username",
            "gns3.port",
            "esxi.gns3_vm_ova_path",
            "esxi.gns3_vm_mgmt_network",
            "esxi.gns3_vm_mac_range",
        ],
    ),
    ("API services", [key for key in FIELDS if key.startswith("api.")]),
]
HELP = {
    "log_file_path": "Path on the server for application logs.",
    "generate_max_retries": "Retries after the first unsuccessful generation attempt.",
    "incremental_deploy": "Keep enabled to reuse existing devices. Disabling resets networking and redeploys the topology.",
    "esxi.delete_unused_vms": "During a full deployment, delete tool-created VMs absent from the new topology.",
    "esxi.ignore_port_groups": "One per line. Include the GNS3 management port group to protect it during full deployments.",
    "esxi.datastore": "Leave blank to select the datastore with most free space.",
    "esxi.gns3_vm_ova_path": "Optional server path. With a management network, enables fresh GNS3 VM deployment and recovery when missing.",
    "esxi.gns3_vm_mgmt_network": "Management port group for the GNS3 VM's first network adapter.",
    "esxi.gns3_vm_mac_range": "Optional inclusive range: start and end MAC addresses, one per line.",
    "gns3.parent_interface": "Leave blank to detect the trunk interface by MAC address.",
    "esxi.virtual_switch": "The vSwitch containing the GNS3 trunk port group.",
    "esxi.trunk_port_group": "The port group connected to the GNS3 VM's trunk adapter.",
}


def field_string(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (list, tuple, set)):
        return "\n".join(sorted(value) if isinstance(value, set) else value)
    return str(value)


def schema():
    return [
        {
            "title": title,
            "fields": [
                {
                    "key": key,
                    "default": field_string(FIELDS[key]),
                    "optional": FIELDS[key] is None or isinstance(FIELDS[key], set),
                    "type": "boolean"
                    if isinstance(FIELDS[key], bool)
                    else "number"
                    if isinstance(FIELDS[key], int)
                    else "list"
                    if key in ("esxi.ignore_port_groups", "esxi.gns3_vm_mac_range")
                    else "url"
                    if key.startswith("api.")
                    else "text",
                    "help": HELP.get(key, ""),
                }
                for key in keys
            ],
        }
        for title, keys in GROUPS
    ]


def build_document(values):
    defaults = {key: field_string(value) for key, value in FIELDS.items()}
    defaults.update(values)
    fields = validate_fields(defaults)
    if bool(fields["esxi.gns3_vm_ova_path"]) != bool(
        fields["esxi.gns3_vm_mgmt_network"]
    ):
        raise ValueError(
            "GNS3 VM deployment needs both an OVA path and a management network"
        )
    data = {}
    for key, value in fields.items():
        section, _, name = key.rpartition(".")
        target = data.setdefault(section, {}) if section else data
        target[name] = value
    return data


def render_config(values):
    return yaml.safe_dump(build_document(values), sort_keys=False)


def create_profile(filename, values):
    settings_profiles._validate_name(filename)
    if filename == settings_profiles._active_settings_name():
        raise FileExistsError("A profile with that name already exists")
    content = render_config(values)
    path = settings_profiles._profiles_dir() / filename
    with path.open("x") as stream:
        stream.write(content)
    return {"status": "created", "filename": filename}


def read_document(filename):
    try:
        data = (
            yaml.safe_load(settings_profiles.profile_path(filename).read_text()) or {}
        )
    except yaml.YAMLError as exc:
        raise ValueError("Profile contains invalid YAML") from exc
    if not isinstance(data, dict) or any(
        not isinstance(data.get(section, {}), dict)
        for section in ("esxi", "gns3", "api")
    ):
        raise ValueError("Profile and its esxi, gns3 and api sections must be mappings")
    return data


def read_fields(filename):
    data = read_document(filename)
    result = {}
    for key, default in FIELDS.items():
        section, _, name = key.rpartition(".")
        value = (
            data.get(section, {}).get(name, default)
            if section
            else data.get(name, default)
        )
        result[key] = field_string(value)
    return result


def validate_fields(values):
    result = {}
    for key, value in values.items():
        if key not in FIELDS or not isinstance(value, str):
            raise ValueError(f"Unsupported settings field: {key}")
        value = value.strip()
        if not value and FIELDS[key] is not None and key != "esxi.ignore_port_groups":
            raise ValueError(f"{key} is required")
        if isinstance(FIELDS[key], bool):
            if value.lower() not in ("true", "false"):
                raise ValueError(f"{key} must be true or false")
            value = value.lower() == "true"
        elif key in ("esxi.ignore_port_groups", "esxi.gns3_vm_mac_range"):
            value = [
                part.strip()
                for part in value.replace(",", "\n").splitlines()
                if part.strip()
            ]
            if key.endswith("mac_range"):
                if value:
                    if len(value) != 2 or any(
                        not re.fullmatch(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", mac)
                        for mac in value
                    ):
                        raise ValueError(
                            "MAC range needs two colon-separated MAC addresses"
                        )
                    if int(value[0].replace(":", ""), 16) > int(
                        value[1].replace(":", ""), 16
                    ):
                        raise ValueError("MAC range start must not exceed its end")
                else:
                    value = None
        elif key == "generate_max_retries":
            try:
                value = int(value)
            except ValueError:
                raise ValueError("Generation retries must be a whole number") from None
            if value < 0:
                raise ValueError("Generation retries cannot be negative")
        elif not value and FIELDS[key] is None:
            value = None
        elif key.endswith(".port"):
            try:
                value = int(value)
            except ValueError:
                raise ValueError(f"{key} must be a port number") from None
            if not 1 <= value <= 65535:
                raise ValueError(f"{key} must be between 1 and 65535")
        elif key.startswith("api."):
            parsed = urlsplit(value)
            if (
                parsed.scheme not in ("http", "https")
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError(
                    f"{key} must be an HTTP(S) URL without credentials, query or fragment"
                )
            _ = parsed.port  # Validate an explicitly supplied port.
            value = value.rstrip("/")
        elif key == "esxi.ip" and any(c in value for c in "/@?# \t\n"):
            raise ValueError("ESXi host must be an IP address or hostname")
        result[key] = value
    return result


def update_fields(filename, values):
    data = read_document(filename)
    for key, value in validate_fields(values).items():
        section, _, name = key.rpartition(".")
        target = data.setdefault(section, {}) if section else data
        target[name] = value
    path = settings_profiles.profile_path(filename)
    # Replace atomically, retaining the existing file's permissions.
    import os
    import tempfile

    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".settings-")
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(yaml.safe_dump(data, sort_keys=False))
        os.chmod(temporary, path.stat().st_mode & 0o777)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _probe(item):
    label, address, port = item
    started = time.monotonic()
    result = {
        "service": label,
        "address": address,
        "check": "TCP" if port is not None else "HTTP GET",
    }
    try:
        if port is not None:
            with socket.create_connection((address, port), timeout=3):
                pass
            result.update(
                status="reachable",
                detail=f"TCP port {port} accepts connections; credentials not checked.",
            )
        else:
            # Stream headers only, and do not follow a redirect to another host.
            with requests.get(
                address, timeout=(3, 3), stream=True, allow_redirects=False
            ) as response:
                code = response.status_code
            result.update(
                status="reachable" if 200 <= code < 300 else "warning",
                detail=f"HTTP {code}. "
                + (
                    {
                        401: "Authentication is required.",
                        403: "Access was refused.",
                        404: "Server responded, but the base path has no route; this does not prove the API is down.",
                    }.get(
                        code,
                        "Server error; inspect service logs."
                        if code >= 500
                        else "Server responded.",
                    )
                )
                + " API operations and credentials not checked.",
            )
    except socket.gaierror:
        result.update(
            status="error",
            detail="Hostname could not be resolved. Check its spelling and the server's DNS configuration.",
        )
    except ConnectionRefusedError:
        result.update(
            status="error",
            detail="Connection refused. Check that the service is running and listening on the configured port.",
        )
    except (requests.Timeout, TimeoutError):
        result.update(
            status="error",
            detail="Timed out. Check the address, firewall rules and whether the service is running.",
        )
    except requests.exceptions.SSLError:
        result.update(
            status="error",
            detail="TLS certificate verification failed. Check the certificate hostname and server trust chain.",
        )
    except (OSError, requests.RequestException) as exc:
        result.update(
            status="error",
            detail=f"Connection failed ({type(exc).__name__}); check address, port and network access.",
        )
    result["duration_ms"] = round((time.monotonic() - started) * 1000)
    return result


def test_connections(filename, values, gns3_ip=None):
    fields = (
        read_fields(filename)
        if filename
        else {key: field_string(value) for key, value in FIELDS.items()}
    )
    fields.update(values)
    fields = validate_fields(fields)
    targets = [("ESXi", fields["esxi.ip"], fields["esxi.port"])]
    targets.extend(
        (label, fields[key], None)
        for label, key in (
            ("ESXi templates", "api.esxi_template_server_url"),
            ("GNS3 templates", "api.gns3_template_server_url"),
            ("Topology generator", "api.topology_generator_url"),
            ("OVA deployment", "api.ova_deploy_url"),
        )
    )
    if (
        gns3_ip
        and fields["esxi.ip"] == Settings.ESXI.IP
        and fields["esxi.gns3_vm_name"] == Settings.ESXI.GNS3_VM_NAME
    ):
        targets.append(("GNS3", gns3_ip, fields["gns3.port"]))
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(_probe, targets))
    if len(targets) == 5:
        results.append(
            {
                "service": "GNS3",
                "address": "",
                "status": "skipped",
                "detail": "VM address unavailable for this host. Activate it to discover the GNS3 address.",
            }
        )
    return results
