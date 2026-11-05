#!/usr/bin/env python3
"""Find the lab's machines on the home network (Terraform's external data source).

Reads {"server": name, "windows": name, "nas": name, "ssh": "server,windows"}
on stdin and prints {"server": ip, "windows": ip, "nas": ip}. Each name is
resolved the way this Mac resolves it (the main router's DNS answers the .lan
names); each machine named in "ssh" must accept a TCP connection on port 22,
which is what bootstrap-linux.sh and bootstrap-windows.ps1 open. Anything
missing ends the run with a message saying what to do, before Terraform
touches a machine. A blank name is skipped (no Windows worker, say).

Run by hand to see the network the way Terraform will:
    echo '{"server":"omarchy.lan","windows":"Eds9950X.lan","nas":"DXP8800PLUS-76B2.lan","ssh":"server,windows"}' \\
        | python3 deploy/home-lab/scripts/discover.py
"""

import json
import socket
import sys

FIX = {
    "server": "on the Linux machine run deploy/home-lab/bootstrap-linux.sh with sudo (sshd and the ufw rules)",
    "windows": "on the Windows PC run deploy/home-lab/bootstrap-windows.ps1 as administrator (OpenSSH Server, Private profile)",
    "nas": "check the NAS is on and its name in terraform.tfvars",
}


def resolve(name: str) -> str:
    try:
        infos = socket.getaddrinfo(name, None, socket.AF_INET, socket.SOCK_STREAM)
    except socket.gaierror:
        return ""
    return infos[0][4][0] if infos else ""


def port_open(ip: str, port: int) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=3):
            return True
    except OSError:
        return False


def main() -> int:
    query = json.load(sys.stdin)
    wants_ssh = {k for k in query.get("ssh", "").split(",") if k}
    found, problems = {}, []
    for role in ("server", "windows", "nas"):
        name = (query.get(role) or "").strip()
        if not name:
            continue
        ip = resolve(name)
        if not ip:
            problems.append(f"{role}: {name} does not resolve; is it on, and is the name right? ({FIX[role]})")
            continue
        found[role] = ip
        if role in wants_ssh and not port_open(ip, 22):
            problems.append(f"{role}: {name} ({ip}) does not accept SSH on port 22; {FIX[role]}")
    if problems:
        print("the lab's machines are not all reachable:\n  " + "\n  ".join(problems), file=sys.stderr)
        return 1
    print(json.dumps(found))
    return 0


if __name__ == "__main__":
    sys.exit(main())
