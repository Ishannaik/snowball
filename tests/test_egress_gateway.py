#!/usr/bin/env python3
"""Tests for the Enforcing Egress Gateway.

Verifies destination pinning, SSRF deny list, port restrictions, approved origin
filtering, upstream proxy override rejection, and network namespace isolation detection.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import socket
import sys
import unittest

ROOT = Path(__file__).resolve().parent.parent
_TOOLS = ROOT / "tools"
sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != _TOOLS]
if (
    getattr(sys.modules.get("queue"), "__file__", None)
    and Path(sys.modules["queue"].__file__).resolve().parent == _TOOLS
):
    del sys.modules["queue"]


def _load(filename: str, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, _TOOLS / filename)
    if spec is None or spec.loader is None:
        raise RuntimeError(filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


guard = _load("egress_guard.py", "snowball_egress_guard")
gateway = guard


class EgressGatewayTests(unittest.TestCase):
    def test_gateway_lifecycle_and_port_binding(self):
        with gateway.EgressGateway() as gw:
            self.assertGreater(gw.port, 0)
            self.assertEqual(gw.url, f"http://127.0.0.1:{gw.port}")
            self.assertFalse(gw._closed)
        self.assertTrue(gw._closed)

    def test_connect_to_private_host_denied_ssrf(self):
        def resolver(host: str, _port: int) -> list[str]:
            if host == "local.example":
                return ["127.0.0.1"]
            if host == "metadata.example":
                return ["169.254.169.254"]
            if host == "private10.example":
                return ["10.1.2.3"]
            if host == "cgnat.example":
                return ["100.64.0.1"]
            return ["192.168.1.1"]

        with gateway.EgressGateway(resolver=resolver) as gw:
            for host in ("local.example", "metadata.example", "private10.example", "cgnat.example"):
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.connect(("127.0.0.1", gw.port))
                    s.sendall(f"CONNECT {host}:443 HTTP/1.1\r\n\r\n".encode("utf-8"))
                    resp = s.recv(1024)
                    self.assertIn(b"403 Forbidden", resp, f"Expected 403 Forbidden for {host}")

    def test_connect_to_disallowed_ports_denied(self):
        def resolver(_host: str, _port: int) -> list[str]:
            return ["203.0.113.9"]

        with gateway.EgressGateway(resolver=resolver) as gw:
            for bad_port in (22, 25, 8080, 8443, 3000):
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.connect(("127.0.0.1", gw.port))
                    s.sendall(f"CONNECT grants.example.org:{bad_port} HTTP/1.1\r\n\r\n".encode("utf-8"))
                    resp = s.recv(1024)
                    self.assertIn(b"403 Forbidden", resp, f"Expected 403 Forbidden for port {bad_port}")

    def test_connect_to_ip_literal_denied(self):
        with gateway.EgressGateway() as gw:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.connect(("127.0.0.1", gw.port))
                s.sendall(b"CONNECT 93.184.216.34:443 HTTP/1.1\r\n\r\n")
                resp = s.recv(1024)
                self.assertIn(b"403 Forbidden", resp)

    def test_dns_rebinding_mixed_answers_denied(self):
        def rebinding_resolver(_host: str, _port: int) -> list[str]:
            # Returns both a public IP and a private IP (rebinding attempt)
            return ["203.0.113.9", "127.0.0.1"]

        with gateway.EgressGateway(resolver=rebinding_resolver) as gw:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.connect(("127.0.0.1", gw.port))
                s.sendall(b"CONNECT rebinding.example.org:443 HTTP/1.1\r\n\r\n")
                resp = s.recv(1024)
                self.assertIn(b"403 Forbidden", resp)

    def test_approved_origins_enforcement(self):
        def resolver(_host: str, _port: int) -> list[str]:
            return ["203.0.113.9"]

        approved = ("https://grants.example.org", "https://api.example.org")
        with gateway.EgressGateway(approved_origins=approved, resolver=resolver) as gw:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.connect(("127.0.0.1", gw.port))
                s.sendall(b"CONNECT unauthorized.example.org:443 HTTP/1.1\r\n\r\n")
                resp = s.recv(1024)
                self.assertIn(b"403 Forbidden", resp)
                self.assertIn(b"origin_not_approved", resp)

            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s2:
                s2.connect(("127.0.0.1", gw.port))
                s2.sendall(b"CONNECT grants.example.org.evil:443 HTTP/1.1\r\n\r\n")
                resp2 = s2.recv(1024)
                self.assertIn(b"403 Forbidden", resp2)
                self.assertIn(b"origin_not_approved", resp2)
    def test_upstream_proxy_override_rejected(self):
        def resolver(_host: str, _port: int) -> list[str]:
            return ["203.0.113.9"]

        with gateway.EgressGateway(upstream_proxy="http://customer-proxy:pass@p.example:8080", resolver=resolver) as gw:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.connect(("127.0.0.1", gw.port))
                s.sendall(b"CONNECT grants.example.org:443 HTTP/1.1\r\n\r\n")
                resp = s.recv(1024)
                self.assertIn(b"403 Forbidden", resp)
                self.assertIn(b"upstream_proxy_overrides_pin", resp)

    def test_plain_http_method_forwarding_and_filtering(self):
        def resolver(_host: str, _port: int) -> list[str]:
            return ["127.0.0.1"]

        with gateway.EgressGateway(resolver=resolver) as gw:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.connect(("127.0.0.1", gw.port))
                s.sendall(b"GET http://internal.example/secret HTTP/1.1\r\nHost: internal.example\r\n\r\n")
                resp = s.recv(1024)
                self.assertIn(b"403 Forbidden", resp)

    def test_namespace_isolation_contract(self):
        ok, reason = guard.check_namespace_isolation()
        self.assertIsInstance(ok, bool)
        if not ok:
            self.assertIn(reason, ("network_namespace_unsupported", "network_namespace_not_active"))

    def test_isolation_prerequisites_flags_environment_proxy(self):
        ok, reason = guard.isolation_prerequisites({"HTTP_PROXY": "http://evil:8080"})
        self.assertFalse(ok)
        self.assertEqual(reason, "environment_proxy")


if __name__ == "__main__":
    unittest.main()
