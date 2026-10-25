"""Keep integration calls away from addresses a connection must never reach.

Connection URLs (a remote service, an MCP server, a GitHub Enterprise or Jira base
URL) are set by tenants, so an outbound call could otherwise be pointed at the
cloud metadata service and read this deployment's credentials. Every request made
through ``ConnectionContext.http()`` is checked against
``SHOAL_INTEGRATION_BLOCKED_NETWORKS`` (by default the link-local and metadata
ranges; add private ranges for a multi-tenant deployment that should not reach its
own network).
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Iterable

import httpx

from shoal.integrations.base import IntegrationError

DEFAULT_BLOCKED_NETWORKS = [
    "169.254.0.0/16",  # link-local, incl. AWS/GCP/Azure metadata at 169.254.169.254
    "100.100.100.200/32",  # Alibaba Cloud metadata
    "fe80::/10",  # IPv6 link-local
    "fd00:ec2::254/128",  # AWS metadata over IPv6
]

Network = ipaddress.IPv4Network | ipaddress.IPv6Network


def networks(cidrs: Iterable[str]) -> list[Network]:
    return [ipaddress.ip_network(c, strict=False) for c in cidrs]


def _blocked(address: str, blocked: list[Network]) -> bool:
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return any(ip.version == net.version and ip in net for net in blocked)


async def _addresses(host: str, port: int) -> list[str]:
    try:
        return [str(ipaddress.ip_address(host.strip("[]")))]
    except ValueError:
        pass
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return []  # unresolvable: the request itself fails with a clear connection error
    return [str(info[4][0]) for info in infos]


def guard(blocked: list[Network]) -> Callable[[httpx.Request], Awaitable[None]]:
    """An httpx request hook that refuses hosts resolving into ``blocked`` (also applied to each redirect)."""

    async def check(request: httpx.Request) -> None:
        url = request.url
        for address in await _addresses(url.host, url.port or (443 if url.scheme == "https" else 80)):
            if _blocked(address, blocked):
                raise IntegrationError(
                    f"refusing to call {url.scheme}://{url.host}: it resolves to {address}, a blocked address "
                    "(SHOAL_INTEGRATION_BLOCKED_NETWORKS)",
                    status=403,
                )

    return check
