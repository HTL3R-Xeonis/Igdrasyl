"""
Measures what the GNS3 VM does while real traffic crosses the lab (run it ON THE TEST BOX).

    ~/TopologyOperator-livetest/.venv/bin/python /tmp/gns3_vm_load_probe.py > /tmp/gns3diag.out

What it does, in this order, and always cleans up at the end:
  1. gives LXWEB, LXDB and LXMON temporary addresses (the same commands as the connectivity test),
  2. starts a 1400-byte, 100-packets-per-second ping stream LXWEB -> LXDB in the background,
  3. logs in to the GNS3 VM over SSH (user and password come from the template API, entry
     "gns3-vm"; the password is used only for this login and is NEVER printed or written),
  4. prints facts and per-thread CPU load while the stream runs (read-only commands, fixed list),
  5. removes the temporary addresses again.

Nothing on the GNS3 VM is changed: only `nproc`, `ls /dev/kvm`, `ps`, `top` and `cat /proc/...`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request

os.chdir(os.path.expanduser("~/TopologyOperator-livetest"))
sys.path.insert(0, ".")
import src.connections  # noqa: F401 - import order (circular import otherwise)

import app.connectivity_test as ct

TEMPLATE_API = "http://10.20.20.171:8000/api/templates"
GNS3_VM = "10.20.20.227"
STREAM_SECONDS = 70

HOSTS = [
    ct.Host("LXWEB", "198.18.200.1", False, 0),
    ct.Host("LXDB", "198.18.200.2", False, 0),
    ct.Host("LXMON", "198.18.200.3", False, 0),
]

READ_ONLY_COMMANDS = [
    ("cpus", "nproc; grep -m1 'model name' /proc/cpuinfo"),
    ("kvm device", "ls -l /dev/kvm; cat /sys/module/kvm_intel/parameters/nested 2>&1"),
    (
        "qemu processes (command line, trimmed)",
        "ps -eo pid,pcpu,etime,args --sort=-pcpu | grep -E '[q]emu|[u]bridge' | cut -c1-230 | head -12",
    ),
    (
        "threads by CPU, 3 samples of 10 s",
        "top -b -H -n 3 -d 10 -w 200 | grep -E '^top|^%Cpu|qemu|ubridge|ksoftirq|vhost' | head -60",
    ),
    ("softirq and interrupts", "grep -E 'NET_RX|NET_TX' /proc/softirqs"),
]


def push(devices):
    process = subprocess.run(
        ["python3", "/tmp/push.py"],
        input=json.dumps(devices),
        capture_output=True,
        check=False,
        text=True,
    )
    return json.loads(process.stdout)


def gns3_credentials():
    data = json.load(urllib.request.urlopen(TEMPLATE_API, timeout=15))
    for template in data["templates"]:
        if template["name"] == "gns3-vm":
            return template["users"][0], template["passwords"][0]
    raise SystemExit("no 'gns3-vm' entry in the template API")


def main():
    import paramiko

    user, password = gns3_credentials()
    print("== setting temporary addresses", flush=True)
    push({host.name: ct.setup_commands(host) for host in HOSTS})
    try:
        print(
            f"== starting {STREAM_SECONDS} s of 1400 B x 100 pps, LXWEB -> LXDB",
            flush=True,
        )
        stream = (
            f"setsid nohup ping -w {STREAM_SECONDS} -i 0.01 -s 1400 -q {HOSTS[1].address} "
            "> /tmp/probe_stream.txt 2>&1 < /dev/null &"
        )
        push({"LXWEB": {"commands": [stream], "continue_on_error": True}})
        time.sleep(5)  # let the stream build up before measuring

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            GNS3_VM,
            username=user,
            password=password,
            timeout=15,
            allow_agent=False,
            look_for_keys=False,
        )
        del password
        try:
            for title, command in READ_ONLY_COMMANDS:
                print(f"\n== {title}\n$ {command}", flush=True)
                _, stdout, stderr = client.exec_command(command, timeout=60)
                print(stdout.read().decode(errors="replace"), end="")
                error = stderr.read().decode(errors="replace").strip()
                if error:
                    print("[stderr]", error)
        finally:
            client.close()

        time.sleep(max(0, STREAM_SECONDS - 40))
        result = push(
            {
                "LXWEB": {
                    "commands": ["cat /tmp/probe_stream.txt"],
                    "continue_on_error": True,
                }
            }
        )
        print("\n== result of the ping stream")
        print(result["results"][0]["commands"][0]["output"])
    finally:
        print("== removing temporary addresses", flush=True)
        push(
            {
                host.name: {
                    "commands": ct.cleanup_commands(host)
                    + ["rm -f /tmp/probe_stream.txt"],
                    "continue_on_error": True,
                }
                for host in HOSTS
            }
        )
        print("== done")


if __name__ == "__main__":
    main()
