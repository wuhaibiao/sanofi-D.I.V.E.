#!/usr/bin/env python3
"""Remove the former memsys deployment on its Linux server, keeping data volumes.

Standalone: no project dependencies, Compose files or provider credentials needed.
The default prints a plan; --apply removes only the former memsys deployment.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys

PROJECT = "memsys"
DOMAIN = "memory.whb.vin"
SERVICES = {"memory", "qdrant", "embedding", "rerank", "gateway"}
CADDYFILE = Path("/opt/whb-wordpress/Caddyfile")
DEPLOY = Path("/opt/memsys/deploy")
ARCHIVES = (
    "memsys-cloud-20261012.tar.gz",
    "memsys-cloud-additive-20261012.tar.gz",
    "memsys-cloud-mem0-20261012.tar.gz",
)


def run(args, **kwargs):
    return subprocess.run(args, text=True, check=True, timeout=60, **kwargs)


def managed_site_removed(original):
    lines = original.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if line.strip() == "# BEGIN memsys " + DOMAIN]
    ends = [i for i, line in enumerate(lines) if line.strip() == "# END memsys " + DOMAIN]
    if not starts and not ends:
        candidate = original
    elif len(starts) != 1 or len(ends) != 1 or ends[0] <= starts[0]:
        raise ValueError("Incomplete/duplicate memsys Caddy markers; no changes made")
    else:
        candidate = "".join(lines[:starts[0]] + lines[ends[0] + 1:])
    if re.search(r"(?<![A-Za-z0-9.-])" + re.escape(DOMAIN) + r"(?![A-Za-z0-9.-])", candidate):
        raise ValueError("Unmanaged memory hostname remains; edit that site separately")
    return candidate


def caddy(command, content=None):
    run(["docker", "exec", "-i", "-w", "/etc/caddy", "whb_caddy", "caddy", command,
         "--config", "-" if content is not None else "/etc/caddy/Caddyfile",
         "--adapter", "caddyfile"], input=content)


def write_mounted(stream, content):
    # Keep the inode so the running container's single-file bind mount updates.
    stream.seek(0)
    stream.write(content)
    stream.truncate()
    stream.flush()
    os.fsync(stream.fileno())


def remove_site(path):
    with path.open("r+", newline="") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        original = stream.read()
        candidate = managed_site_removed(original)
        if candidate == original:
            print("Memory Caddy site already absent")
            return
        caddy("validate", candidate)
        stream.seek(0)
        if stream.read() != original or path.stat().st_ino != os.fstat(stream.fileno()).st_ino:
            raise RuntimeError("Caddyfile changed during validation; no changes applied")
        backup = path.with_name("Caddyfile.before-memsys-removal-" + secrets.token_hex(8))
        fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", newline="") as target:
            target.write(original)
        try:
            write_mounted(stream, candidate)
            caddy("reload")
        except BaseException:
            write_mounted(stream, original)
            try:
                caddy("reload")
            except Exception:
                print("Previous Caddyfile restored; reload failed, inspect whb_caddy")
            raise
        print("Memory site removed; Caddyfile backup: " + str(backup))


def containers():
    ids = run(["docker", "ps", "-aq", "--filter", "label=com.docker.compose.project=" + PROJECT],
              capture_output=True).stdout.split()
    if not ids:
        return []
    items = json.loads(run(["docker", "inspect", *ids], capture_output=True).stdout)
    for item in items:
        labels = item["Config"].get("Labels") or {}
        directory = labels.get("com.docker.compose.project.working_dir")
        if (labels.get("com.docker.compose.project") != PROJECT
                or labels.get("com.docker.compose.service") not in SERVICES
                or directory not in ("/opt/memsys", "/opt/memsys/deploy")):
            identity = {"name": item.get("Name"), "project": labels.get("com.docker.compose.project"),
                        "service": labels.get("com.docker.compose.service"), "working_dir": directory}
            raise ValueError("Unexpected memsys container identity " + json.dumps(identity) + "; no changes made")
    return items


def remove_empty_networks():
    ids = run(["docker", "network", "ls", "-q", "--filter",
               "label=com.docker.compose.project=" + PROJECT], capture_output=True).stdout.split()
    for network_id in ids:
        item = json.loads(run(["docker", "network", "inspect", network_id], capture_output=True).stdout)[0]
        if (item.get("Labels") or {}).get("com.docker.compose.project") == PROJECT and not item.get("Containers"):
            run(["docker", "network", "rm", network_id])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if sys.platform != "linux" or os.geteuid() != 0:
        parser.exit(1, "Run this script as root on the former Linux cloud server, not on the Mac.\n")
    try:
        # Complete identity/config checks before changing the shared gateway.
        for path in (CADDYFILE, DEPLOY):
            if path.exists() and path.resolve() != path:
                raise ValueError("Unexpected symlink in deployment paths; no changes made")
        original = CADDYFILE.read_text() if CADDYFILE.exists() else ""
        managed_site_removed(original)
        items = containers()
        print("Containers: " + (", ".join(item["Name"].lstrip("/") for item in items) or "none"))
        print("Remove managed memory site, /opt/memsys/deploy, and the three old /tmp deployment archives.")
        print("Keep Docker data/model volumes, blog, New API, shared Caddy and its certificates.")
        if not args.apply:
            print("Plan only. Add --apply to perform this cleanup.")
            return
        if CADDYFILE.exists():
            remove_site(CADDYFILE)
        ids = [item["Id"] for item in items]
        if ids:
            run(["docker", "stop", "--time", "30", *ids])
            run(["docker", "rm", *ids])
        remove_empty_networks()
        if DEPLOY.exists():
            shutil.rmtree(DEPLOY)
        for name in ARCHIVES:
            (Path("/tmp") / name).unlink(missing_ok=True)
        print("Cloud deployment configuration removed. Data volumes retained.")
        print("Delete the memory DNS A/AAAA records in your DNS console if present; keep @ and www.")
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        parser.exit(1, "Cleanup stopped: " + str(exc) + "\n")


if __name__ == "__main__":
    main()
