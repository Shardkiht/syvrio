"""Optional local hostname -> loopback mapping for the private Web client.

Deliberately small: append one line to the OS hosts file with a backup, never
touch DNS, never open a public port, and degrade to a manual instruction when
the process lacks permission to write hosts. The Web server still listens on
loopback only; the browser reaches it through the mapped hostname.
"""
from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DOMAIN = "syvrio.local"
LOOPBACK = "127.0.0.1"

# A plain hostname only: no scheme, port, path, wildcard or IP literal.
DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$"
)


def normalize_domain(value: str) -> str:
    domain = value.strip().lower().rstrip(".")
    if not DOMAIN_RE.fullmatch(domain):
        raise ValueError(
            "Domain must be a plain hostname such as syvrio.local "
            "(no scheme, port, path or IP address)."
        )
    # Numeric labels pass the hostname grammar; an IP literal is not a domain.
    try:
        ipaddress.ip_address(domain)
    except ValueError:
        pass
    else:
        raise ValueError("An IP address is not a domain; use a hostname such as syvrio.local.")
    return domain


def hosts_path() -> Path:
    if os.name == "nt":
        root = os.environ.get("SystemRoot", r"C:\Windows")
        return Path(root) / "System32" / "drivers" / "etc" / "hosts"
    return Path("/etc/hosts")


def mapping_line(domain: str, ip: str = LOOPBACK) -> str:
    return f"{ip}\t{normalize_domain(domain)}"


@dataclass(frozen=True)
class HostsResult:
    domain: str
    ip: str
    path: Path
    changed: bool
    backup: Path | None = None
    message: str = ""


def _active_rows(text: str) -> list[tuple[str, list[str]]]:
    rows: list[tuple[str, list[str]]] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        rows.append((parts[0], parts[1:]))
    return rows


def ensure_hosts_mapping(domain: str, ip: str = LOOPBACK, *, path: Path | None = None) -> HostsResult:
    """Add ``ip domain`` once. Idempotent; never rewrites another mapping."""
    domain = normalize_domain(domain)
    target = path or hosts_path()
    try:
        data = target.read_bytes()
    except OSError as exc:
        return HostsResult(domain, ip, target, False,
                           message=f"Cannot read the hosts file ({exc}). "
                                   f"Add manually: {mapping_line(domain, ip)}")
    for address, names in _active_rows(data.decode("utf-8", errors="replace")):
        if domain in names:
            if address == ip:
                return HostsResult(domain, ip, target, False, message="Already mapped.")
            return HostsResult(
                domain, ip, target, False,
                message=f"{domain} is already mapped to {address}; resolve that entry manually.",
            )
    separator = "\r\n" if b"\r\n" in data else "\n"
    prefix = "" if data.endswith((b"\n", b"\r")) else separator
    payload = (prefix + mapping_line(domain, ip) + separator).encode("ascii")
    backup = target.with_name(target.name + ".syvrio-backup")
    try:
        backup.write_bytes(data)
        with target.open("ab") as handle:
            handle.write(payload)
    except PermissionError:
        return HostsResult(
            domain, ip, target, False,
            message="Administrator/root permission is required to edit the hosts file. "
                    f"Add manually: {mapping_line(domain, ip)}",
        )
    except OSError as exc:
        return HostsResult(domain, ip, target, False,
                           message=f"Could not update the hosts file ({exc}). "
                                   f"Add manually: {mapping_line(domain, ip)}")
    return HostsResult(domain, ip, target, True, backup,
                       message=f"Mapped {domain} -> {ip} (backup: {backup.name}).")


def remove_hosts_mapping(domain: str, ip: str = LOOPBACK, *, path: Path | None = None) -> HostsResult:
    """Remove the matching ``ip domain`` line (including an identical manual entry); keep everything else."""
    domain = normalize_domain(domain)
    target = path or hosts_path()
    try:
        data = target.read_bytes()
    except OSError as exc:
        return HostsResult(domain, ip, target, False,
                           message=f"Cannot read the hosts file ({exc}).")
    lines = data.splitlines(keepends=True)
    kept: list[bytes] = []
    removed = False
    for line in lines:
        text = line.decode("utf-8", errors="replace")
        active = text.split("#", 1)[0].strip().split()
        if len(active) >= 2 and active[0] == ip and domain in active[1:]:
            removed = True
            continue
        kept.append(line)
    if not removed:
        return HostsResult(domain, ip, target, False, message="No matching mapping found.")
    backup = target.with_name(target.name + ".syvrio-backup")
    try:
        backup.write_bytes(data)
        target.write_bytes(b"".join(kept))
    except PermissionError:
        return HostsResult(domain, ip, target, False,
                           message="Administrator/root permission is required to edit the hosts file.")
    except OSError as exc:
        return HostsResult(domain, ip, target, False,
                           message=f"Could not update the hosts file ({exc}).")
    return HostsResult(domain, ip, target, True, backup,
                       message=f"Removed {domain} -> {ip} (backup: {backup.name}).")
