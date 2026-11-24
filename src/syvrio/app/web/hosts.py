"""私有网页客户端可选的本地主机名到回环地址映射。

先备份系统 hosts 文件，再追加一行映射。保留 DNS 配置和端口设置。
进程无权写入 hosts 时，提供手工操作说明。
网页服务继续只监听回环地址，浏览器通过映射的主机名访问。
"""

from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DOMAIN = "syvrio.local"
LOOPBACK = "127.0.0.1"

# 只接受主机名，排除协议、端口、路径、通配符和 IP 字面值。
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
    # 纯数字标签也符合主机名语法，需要额外排除 IP 字面值。
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


def ensure_hosts_mapping(
    domain: str, ip: str = LOOPBACK, *, path: Path | None = None
) -> HostsResult:
    """仅追加一次 ip domain，保留其他映射。"""
    domain = normalize_domain(domain)
    target = path or hosts_path()
    try:
        data = target.read_bytes()
    except OSError as exc:
        return HostsResult(
            domain,
            ip,
            target,
            False,
            message=f"Cannot read the hosts file ({exc}). Add manually: {mapping_line(domain, ip)}",
        )
    for address, names in _active_rows(data.decode("utf-8", errors="replace")):
        if domain in names:
            if address == ip:
                return HostsResult(domain, ip, target, False, message="Already mapped.")
            return HostsResult(
                domain,
                ip,
                target,
                False,
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
            domain,
            ip,
            target,
            False,
            message="Administrator/root permission is required to edit the hosts file. "
            f"Add manually: {mapping_line(domain, ip)}",
        )
    except OSError as exc:
        return HostsResult(
            domain,
            ip,
            target,
            False,
            message=f"Could not update the hosts file ({exc}). "
            f"Add manually: {mapping_line(domain, ip)}",
        )
    return HostsResult(
        domain,
        ip,
        target,
        True,
        backup,
        message=f"Mapped {domain} -> {ip} (backup: {backup.name}).",
    )


def remove_hosts_mapping(
    domain: str, ip: str = LOOPBACK, *, path: Path | None = None
) -> HostsResult:
    """移除匹配的 ip domain 行，包括相同的手工条目，其余内容原样保留。"""
    domain = normalize_domain(domain)
    target = path or hosts_path()
    try:
        data = target.read_bytes()
    except OSError as exc:
        return HostsResult(
            domain, ip, target, False, message=f"Cannot read the hosts file ({exc})."
        )
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
        return HostsResult(
            domain,
            ip,
            target,
            False,
            message="Administrator/root permission is required to edit the hosts file.",
        )
    except OSError as exc:
        return HostsResult(
            domain, ip, target, False, message=f"Could not update the hosts file ({exc})."
        )
    return HostsResult(
        domain,
        ip,
        target,
        True,
        backup,
        message=f"Removed {domain} -> {ip} (backup: {backup.name}).",
    )
