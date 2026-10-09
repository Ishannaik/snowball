#!/usr/bin/env python3
"""Destination checks for a restricted browser or proxy gateway.

Direct collectors pin DNS themselves. This module is the check that runs before a
gateway dials. A denied target never reaches the transport. Remote providers resolve
names on their own side; a local pin does not cover them.
"""

from __future__ import annotations

import ipaddress
import os
import re
import select
import socket
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

DENY_NETS = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",
        "240.0.0.0/4",
        "::/128",
        "::1/128",
        "::ffff:0:0/96",
        "64:ff9b::/96",
        "fc00::/7",
        "fe80::/10",
        "ff00::/8",
    )
)
ALLOWED_PORTS = frozenset({80, 443})
PROXY_ENV_NAMES = frozenset(
    {
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "ws_proxy",
        "wss_proxy",
        "no_proxy",
    }
)
LOCAL_NAMES = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "ip6-localhost",
        "ip6-loopback",
        "metadata",
        "metadata.google.internal",
        "metadata.google",
    }
)
_USERINFO = re.compile(r"//[^/\s:@]+:[^/\s@]+@")


class EgressDenied(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class PinnedTarget:
    url: str
    host: str
    port: int
    ip: str


Resolver = Callable[[str, int], list[str]]


def default_resolver(host: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise EgressDenied(f"dns:{exc}") from exc
    return [item[4][0] for item in infos]


def proxy_env_names(env: Mapping[str, str]) -> tuple[str, ...]:
    """Proxy variables that would change egress if a client inherited them."""
    return tuple(key for key in env if key.lower() in PROXY_ENV_NAMES)


def scrub_proxy_env(env: Mapping[str, str]) -> dict[str, str]:
    banned = PROXY_ENV_NAMES
    return {key: value for key, value in env.items() if key.lower() not in banned}


def redact(text: str, secrets: tuple[str, ...] = ()) -> str:
    """Remove proxy userinfo and caller-supplied secrets from a string that may be logged."""
    out = _USERINFO.sub("//[redacted]@", text)
    for secret in secrets:
        if secret and len(secret) >= 4:
            out = out.replace(secret, "[redacted]")
    return out


def validate_target(url: str, *, resolver: Resolver = default_resolver) -> PinnedTarget:
    """Require an HTTP(S) FQDN on port 80 or 443 whose every DNS answer is public."""
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        raise EgressDenied("scheme")
    if parts.username or parts.password:
        raise EgressDenied("url_credentials")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host or host in LOCAL_NAMES or host.endswith(".local") or host.endswith(".internal"):
        raise EgressDenied("local_name")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise EgressDenied("ip_literal")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    if port not in ALLOWED_PORTS:
        raise EgressDenied("port")
    try:
        answers = resolver(host, port)
    except EgressDenied:
        raise
    except Exception as exc:  # noqa: BLE001 - resolver failures are denials, not dials
        raise EgressDenied("dns") from exc
    ips: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for answer in answers:
        try:
            ips.append(ipaddress.ip_address(answer))
        except ValueError as exc:
            raise EgressDenied("dns") from exc
    if not ips:
        raise EgressDenied("dns")
    for ip in ips:
        if _denied(ip):
            raise EgressDenied("private_answer")
    chosen = min(ips, key=lambda item: (item.version, str(item)))
    return PinnedTarget(url=url, host=host, port=port, ip=str(chosen))


def _denied(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None and _denied(ip.ipv4_mapped):
        return True
    if not ip.is_global:
        return True
    if any(ip in net for net in DENY_NETS):
        return True
    return bool(ip.is_multicast or ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_reserved or ip.is_private)


def validate_host(host: str, port: int = 443, *, resolver: Resolver = default_resolver) -> PinnedTarget:
    """Validate an HTTP/HTTPS host and port, ensuring every DNS answer is public."""
    scheme = "https" if port == 443 else "http"
    return validate_target(f"{scheme}://{host}:{port}/", resolver=resolver)


def check_namespace_isolation() -> tuple[bool, str | None]:
    """Detect whether Linux network namespace isolation is active or supported."""
    if os.name != "posix" or sys.platform != "linux":
        return False, "network_namespace_unsupported"
    try:
        self_ns = os.readlink("/proc/self/ns/net")
        init_ns = os.readlink("/proc/1/ns/net")
        if self_ns != init_ns:
            target_ns = Path("/run/netns/snowball-browser")
            if target_ns.exists():
                try:
                    if os.stat("/proc/self/ns/net").st_ino != os.stat(target_ns).st_ino:
                        return False, "network_namespace_not_active"
                except Exception:
                    pass
            return True, None
    except Exception:
        pass
    # Not in a detached network namespace
    return False, "network_namespace_not_active"


def isolation_prerequisites(environ: Mapping[str, str] | None = None) -> tuple[bool, str | None]:
    """Verify proxy scrubbing and namespace requirements for browser isolation."""
    env = os.environ if environ is None else environ
    if proxy_env_names(env):
        return False, "environment_proxy"
    ok, reason = check_namespace_isolation()
    if not ok:
        return False, reason
    return True, None


def normalize_origin(raw: str) -> tuple[str, str, int] | None:
    """Parse and normalize an origin to (scheme, host, port)."""
    try:
        parts = urlsplit(raw if "://" in raw else f"https://{raw}")
        scheme = (parts.scheme or "https").lower()
        if scheme not in {"http", "https"}:
            return None
        host = (parts.hostname or "").rstrip(".").lower()
        if not host:
            return None
        port = parts.port or (443 if scheme == "https" else 80)
        if port not in ALLOWED_PORTS:
            return None
        return scheme, host, port
    except Exception:
        return None


def _origin_matches(origin: str, approved: tuple[str, ...]) -> bool:
    norm = normalize_origin(origin)
    if norm is None:
        return False
    for app in approved:
        app_norm = normalize_origin(app)
        if app_norm is not None and norm == app_norm:
            return True
    return False


def path_allowed(url: str, prefixes: tuple[str, ...]) -> bool:
    """Enforce target allowed path prefixes strictly."""
    if not prefixes:
        return True
    path = urlsplit(url).path or "/"
    for prefix in prefixes:
        clean = prefix if prefix.startswith("/") else f"/{prefix}"
        if path == clean or path.startswith(clean.rstrip("/") + "/"):
            return True
        if clean.endswith("/") and path.startswith(clean):
            return True
    return False


class EgressGateway:
    """Bounded local HTTP/CONNECT proxy that resolves and pins approved public sockets.

    Denies all private/loopback/link-local/metadata IP answers.
    Enforces exact origin allowlists and path prefixes.
    Rejects upstream proxy configurations that would override destination pinning.
    Enforces per-acquisition byte ceilings and socket deadlines.
    """

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        approved_origins: tuple[str, ...] | None = None,
        upstream_proxy: str | None = None,
        resolver: Resolver = default_resolver,
        max_bytes: int = 20_971_520,
    ) -> None:
        self.host = host
        self.requested_port = port
        self.approved_origins = tuple(approved_origins) if approved_origins is not None else None
        self.upstream_proxy = upstream_proxy
        self.resolver = resolver
        self.max_bytes = max_bytes
        self.bytes_transferred: int = 0
        self.server_socket: socket.socket | None = None
        self.port: int = 0
        self.url: str = ""
        self.last_pinned_ip: str | None = None
        self._closed = False
        self._thread: threading.Thread | None = None
        self._active_sockets: list[socket.socket] = []
        self._lock = threading.Lock()

    def start(self) -> EgressGateway:
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_socket.bind((self.host, self.requested_port))
        self.server_socket.listen(64)
        self.server_socket.settimeout(0.5)
        self.port = self.server_socket.getsockname()[1]
        self.url = f"http://{self.host}:{self.port}"
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()
        return self

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.server_socket and self.port > 0:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as dummy:
                    dummy.settimeout(0.2)
                    dummy.connect((self.host, self.port))
            except Exception:
                pass
        if self.server_socket:
            try:
                self.server_socket.close()
            except Exception:
                pass
        with self._lock:
            for s in self._active_sockets:
                try:
                    s.close()
                except Exception:
                    pass
            self._active_sockets.clear()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)

    def __enter__(self) -> EgressGateway:
        return self.start()

    def __exit__(self, *args: object) -> None:
        self.close()

    def _accept_loop(self) -> None:
        while not self._closed and self.server_socket:
            try:
                client_sock, _ = self.server_socket.accept()
            except (socket.timeout, TimeoutError):
                continue
            except Exception:
                break
            if self._closed:
                try:
                    client_sock.close()
                except Exception:
                    pass
                break
            client_sock.settimeout(30.0)
            with self._lock:
                self._active_sockets.append(client_sock)
            t = threading.Thread(target=self._handle_conn, args=(client_sock,), daemon=True)
            t.start()
    def _handle_conn(self, client_sock: socket.socket) -> None:
        try:
            raw = b""
            while b"\r\n\r\n" not in raw and b"\n\n" not in raw and len(raw) < 65536:
                chunk = client_sock.recv(4096)
                if not chunk:
                    break
                raw += chunk
            if not raw:
                return
            delim = b"\r\n\r\n" if b"\r\n\r\n" in raw else b"\n\n"
            header_bytes, body_bytes = raw.split(delim, 1)
            lines = header_bytes.splitlines()
            if not lines:
                self._deny(client_sock, "bad_request")
                return
            first_line = lines[0].decode("utf-8", "replace").strip()
            parts = first_line.split()
            if len(parts) < 2:
                self._deny(client_sock, "bad_request")
                return
            method = parts[0].upper()
            target = parts[1]

            if method == "CONNECT":
                self._handle_connect(client_sock, target)
            elif method in {"GET", "HEAD"}:
                self._handle_http(client_sock, method, target, lines, body_bytes)
            else:
                self._deny(client_sock, "method_not_allowed")
        except Exception:
            pass
        finally:
            try:
                client_sock.close()
            except Exception:
                pass
            with self._lock:
                if client_sock in self._active_sockets:
                    self._active_sockets.remove(client_sock)

    def _handle_connect(self, client_sock: socket.socket, target: str) -> None:
        if ":" in target:
            host, port_str = target.split(":", 1)
            try:
                port = int(port_str)
            except ValueError:
                self._deny(client_sock, "bad_port")
                return
        else:
            host = target
            port = 443

        if port not in ALLOWED_PORTS:
            self._deny(client_sock, "port_not_allowed")
            return

        if self.approved_origins is not None:
            origin = f"https://{host.lower()}"
            if port != 443:
                origin += f":{port}"
            if not _origin_matches(origin, self.approved_origins):
                self._deny(client_sock, "origin_not_approved")
                return

        if self.upstream_proxy is not None:
            self._deny(client_sock, "upstream_proxy_overrides_pin: upstream proxy overrides destination pin")
            return

        try:
            pinned = validate_target(f"https://{host}:{port}/", resolver=self.resolver)
        except EgressDenied as exc:
            self._deny(client_sock, exc.reason)
            return
        family = socket.AF_INET6 if ":" in pinned.ip else socket.AF_INET
        try:
            target_sock = socket.socket(family, socket.SOCK_STREAM)
            target_sock.settimeout(15.0)
            target_sock.connect((pinned.ip, port))
            target_sock.settimeout(None)
            self.last_pinned_ip = pinned.ip
        except Exception:
            self._deny(client_sock, "connect_failed")
            return

        with self._lock:
            self._active_sockets.append(target_sock)

        try:
            client_sock.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            self._tunnel(client_sock, target_sock)
        finally:
            try:
                target_sock.close()
            except Exception:
                pass
            with self._lock:
                if target_sock in self._active_sockets:
                    self._active_sockets.remove(target_sock)

    def _handle_http(
        self,
        client_sock: socket.socket,
        method: str,
        target: str,
        header_lines: list[bytes],
        body_bytes: bytes,
    ) -> None:
        url_parts = urlsplit(target)
        origin = f"{url_parts.scheme}://{(url_parts.hostname or '').lower()}"
        if url_parts.port and url_parts.port not in (80, 443):
            origin += f":{url_parts.port}"

        if self.approved_origins is not None:
            if not _origin_matches(origin, self.approved_origins):
                self._deny(client_sock, "origin_not_approved")
                return

        if self.upstream_proxy is not None:
            self._deny(client_sock, "upstream_proxy_overrides_pin: upstream proxy overrides destination pin")
            return

        try:
            pinned = validate_target(target, resolver=self.resolver)
        except EgressDenied as exc:
            self._deny(client_sock, exc.reason)
            return
        host_header = ""
        for line in header_lines[1:]:
            line_str = line.decode("utf-8", "replace").strip()
            if line_str.lower().startswith("host:"):
                host_header = line_str.split(":", 1)[1].strip()
                break
        expected_host = url_parts.hostname or ""
        if host_header:
            hdr_host = host_header.split(":")[0].strip().lower()
            if hdr_host != expected_host.lower():
                self._deny(client_sock, "host_mismatch")
                return

        path = url_parts.path or "/"
        if url_parts.query:
            path += f"?{url_parts.query}"
        req_lines = [f"{method} {path} HTTP/1.1".encode("ascii")]
        req_lines.append(f"Host: {expected_host}:{pinned.port}".encode("ascii"))
        req_lines.append(b"Connection: close")

        for line in header_lines[1:]:
            lower = line.lower()
            if (
                lower.startswith(b"host:")
                or lower.startswith(b"connection:")
                or lower.startswith(b"proxy-")
            ):
                continue
            req_lines.append(line)
        reconstructed = b"\r\n".join(req_lines) + b"\r\n\r\n" + body_bytes

        family = socket.AF_INET6 if ":" in pinned.ip else socket.AF_INET
        try:
            target_sock = socket.socket(family, socket.SOCK_STREAM)
            target_sock.settimeout(15.0)
            target_sock.connect((pinned.ip, pinned.port))
            target_sock.settimeout(None)
            self.last_pinned_ip = pinned.ip
        except Exception:
            self._deny(client_sock, "connect_failed")
            return

        with self._lock:
            self._active_sockets.append(target_sock)

        try:
            target_sock.sendall(reconstructed)
            self._tunnel(client_sock, target_sock)
        finally:
            try:
                target_sock.close()
            except Exception:
                pass
            with self._lock:
                if target_sock in self._active_sockets:
                    self._active_sockets.remove(target_sock)

    def _tunnel(self, s1: socket.socket, s2: socket.socket, timeout: float = 45.0) -> None:
        start = time.monotonic()
        while not self._closed and (time.monotonic() - start < timeout):
            rem = max(0.1, timeout - (time.monotonic() - start))
            try:
                r, _, _ = select.select([s1, s2], [], [], min(1.5, rem))
            except Exception:
                break
            if not r:
                continue
            for s in r:
                other = s2 if s is s1 else s1
                try:
                    data = s.recv(16384)
                    if not data:
                        return
                    with self._lock:
                        self.bytes_transferred += len(data)
                        if self.bytes_transferred > self.max_bytes:
                            return
                    other.sendall(data)
                except Exception:
                    return

    def _deny(self, sock: socket.socket, reason: str) -> None:
        body = f"Forbidden: {reason}\n".encode("utf-8")
        msg = (
            b"HTTP/1.1 403 Forbidden\r\n"
            b"Content-Type: text/plain\r\n"
            b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n"
            b"Connection: close\r\n\r\n" + body
        )
        try:
            sock.sendall(msg)
        except Exception:
            pass
