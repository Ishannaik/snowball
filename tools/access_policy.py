#!/usr/bin/env python3
"""Domain access policy: public fetch by default, paid and solver only when configured.

Unknown hosts stay on the free public route. Private topics, robots exclusions, login and
paywall barriers, generic access denials, and rate-limit bans never escalate. A recognized
Turnstile or reCAPTCHA v2 challenge can reach the solver only on an allowlisted origin whose
path is approved for that challenge. ZenRows stays off unless the URL's origin and path are
both on that allowlist. Loopback, private, link-local, metadata, reserved, and CGNAT targets
are denied before any mode is considered.

`tools/` is not placed on `sys.path` (tools/queue.py would shadow the stdlib queue module).
"""

from __future__ import annotations

import sys
from pathlib import Path

_TOOLS_DIR = Path(__file__).resolve().parent
sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != _TOOLS_DIR]
if (
    getattr(sys.modules.get("queue"), "__file__", None)
    and Path(sys.modules["queue"].__file__).resolve().parent == _TOOLS_DIR
):
    del sys.modules["queue"]

import hashlib
import ipaddress
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

import yaml

MODE_ORDER = ("public", "paid", "solver")
RECOGNIZED_CHALLENGES = frozenset({"challenge_turnstile", "challenge_recaptcha"})
REQUIRED_TERMINAL = frozenset(
    {
        "login",
        "paywall",
        "generic_401",
        "generic_403",
        "generic_407",
        "generic_451",
        "429",
        "ban",
        "robots",
    }
)
BUILTIN_STATUS = {
    "401": "generic_401",
    "403": "generic_403",
    "407": "generic_407",
    "429": "429",
    "451": "generic_451",
}
_BUDGET_KEYS = frozenset(
    {
        "daily_micro_usd",
        "monthly_micro_usd",
        "daily_retrieval_count",
        "daily_solver_count",
        "monthly_solver_count",
        "tariffs",
        "providers",
    }
)
_PROVIDER_KEYS = frozenset(
    {
        "daily_micro_usd",
        "monthly_micro_usd",
        "daily_count",
        "monthly_count",
        "daily_solver_count",
        "monthly_solver_count",
        "tariff_micro_usd",
    }
)
_LOCAL_NAMES = frozenset(
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
_DENY_NETS = tuple(
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
_DOTTED_IP = re.compile(r"(?:0x[0-9a-f]+|\d+)(?:\.(?:0x[0-9a-f]+|\d+)){0,3}\Z")
_WALL_NAME = re.compile(r"[a-z0-9_]+")
_CACHE_LOCK = threading.Lock()
_CACHE: dict[str, _CacheHit] = {}


class AccessPolicyStale(Exception):
    """Raised when a session is bound to a policy whose file has changed."""


@dataclass(frozen=True)
class AccessDecision:
    allowed_modes: tuple[str, ...]
    denial_reason: str | None
    approved_resource_origins: tuple[str, ...]
    allowed_path_prefixes: tuple[str, ...]


@dataclass(frozen=True)
class DomainRule:
    origin: str
    path_prefixes: tuple[str, ...]
    resource_origins: tuple[str, ...]
    modes: tuple[str, ...]
    zenrows: bool
    challenges: tuple[str, ...]


@dataclass
class _CacheHit:
    fingerprint: str
    mtime_ns: int
    size: int
    policy: object


class AccessPolicy:
    """One parsed access file. Sessions bound to it drop when the file changes."""

    def __init__(
        self,
        *,
        domains: tuple[DomainRule, ...],
        terminal_walls: frozenset[str],
        budgets: dict[str, object],
        source_path: str,
        fingerprint: str,
        mtime_ns: int,
    ) -> None:
        self.domains = domains
        self.rules = {rule.origin: rule for rule in domains}
        self.terminal_walls = terminal_walls
        self._budgets = budgets
        self.source_path = source_path
        self.fingerprint = fingerprint
        self.mtime_ns = mtime_ns
        self._sessions: dict[str, object] = {}
        self._stale = False
        self._lock = threading.Lock()

    def caps(self) -> dict[str, object]:
        """Budget dict accepted by paid_budget.reserve. Values are the configured ceilings."""
        providers = self._budgets["providers"]
        tariffs = self._budgets["tariffs"]
        return {
            "daily_micro_usd": self._budgets["daily_micro_usd"],
            "monthly_micro_usd": self._budgets["monthly_micro_usd"],
            "daily_retrieval_count": self._budgets["daily_retrieval_count"],
            "daily_solver_count": self._budgets["daily_solver_count"],
            "monthly_solver_count": self._budgets["monthly_solver_count"],
            "tariffs": dict(tariffs) if isinstance(tariffs, dict) else {},
            "providers": {name: dict(values) for name, values in providers.items()}
            if isinstance(providers, dict)
            else {},
        }

    @property
    def stale(self) -> bool:
        with self._lock:
            return self._stale

    def bind_session(self, session_id: str, session: object) -> None:
        """Remember a fetch/browser session that must die with this policy generation."""
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("session_id")
        with self._lock:
            if self._stale:
                raise AccessPolicyStale(self.source_path)
            self._sessions[session_id] = session

    def session(self, session_id: str) -> object | None:
        with self._lock:
            if self._stale:
                return None
            return self._sessions.get(session_id)

    def _invalidate(self) -> None:
        with self._lock:
            self._stale = True
            self._sessions.clear()

    def decide(self, url: str, private: bool = False, wall_kind: str | int | None = None) -> AccessDecision:
        """Return the modes still allowed for this URL.

        `denial_reason` is None when nothing restricts the returned modes. A non-empty public
        mode plus a reason means escalation is forbidden; it does not authorize bypassing the
        named barrier. SSRF and path escapes return no modes.
        """
        parsed = _parse_url(url)
        if parsed is None:
            return AccessDecision((), "ssrf_or_path_escape", (), ())
        origin, path = parsed
        wall = _normalize_wall(wall_kind)
        rule = self.rules.get(origin)
        if rule is None:
            return AccessDecision(("public",), _unknown_reason(bool(private), wall), (), ())
        if not _prefix_match(path, rule.path_prefixes):
            return _off_path(rule, bool(private), wall)
        return _on_path(rule, bool(private), wall)


def load_access_policy(path: str | Path) -> AccessPolicy:
    """Load YAML access policy, reusing the cached object until path, mtime, size, or hash changes."""
    file = Path(path)
    data, stat = _read_stable(file)
    digest = hashlib.sha256(data).hexdigest()
    key = str(file.resolve())
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit is not None and _same_hit(hit, digest, stat):
            return hit.policy
    policy = _policy_from_yaml(data, source_path=key, fingerprint=digest, mtime_ns=stat.st_mtime_ns)
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit is not None and _same_hit(hit, digest, stat):
            return hit.policy
        if hit is not None:
            hit.policy._invalidate()
        _CACHE[key] = _CacheHit(digest, stat.st_mtime_ns, stat.st_size, policy)
        return policy


def _same_hit(hit: _CacheHit, digest: str, stat: object) -> bool:
    return hit.fingerprint == digest and hit.mtime_ns == stat.st_mtime_ns and hit.size == stat.st_size


def _read_stable(path: Path) -> tuple[bytes, object]:
    last = path.read_bytes()
    stat = path.stat()
    for _ in range(3):
        before = path.stat()
        data = path.read_bytes()
        after = path.stat()
        if before.st_mtime_ns == after.st_mtime_ns and before.st_size == after.st_size == len(data):
            return data, after
        last = data
        stat = after
    return last, stat


def _policy_from_yaml(data: bytes, *, source_path: str, fingerprint: str, mtime_ns: int) -> AccessPolicy:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("access policy must be utf-8") from exc
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError("access policy YAML is invalid") from exc
    if not isinstance(loaded, dict):
        raise ValueError("access policy must be a mapping")
    return _policy_from_mapping(loaded, source_path=source_path, fingerprint=fingerprint, mtime_ns=mtime_ns)


def _policy_from_mapping(
    loaded: Mapping[str, object], *, source_path: str, fingerprint: str, mtime_ns: int
) -> AccessPolicy:
    if isinstance(loaded.get("version", 1), bool) or loaded.get("version", 1) != 1:
        raise ValueError("unsupported access policy version")
    _reject_widened_unknown(loaded.get("unknown_domains"))
    _reject_challenge_catalog(loaded.get("recognized_challenges"))
    _reject_weakened_statuses(loaded.get("terminal_statuses"))
    terminal = _terminal_walls(loaded.get("terminal_walls"))
    domains = _domains(loaded.get("domains"))
    budgets = _budgets(loaded.get("budgets"))
    return AccessPolicy(
        domains=domains,
        terminal_walls=terminal,
        budgets=budgets,
        source_path=source_path,
        fingerprint=fingerprint,
        mtime_ns=mtime_ns,
    )


def _reject_challenge_catalog(raw: object) -> None:
    if raw is None:
        return
    if not isinstance(raw, list) or set(raw) != RECOGNIZED_CHALLENGES:
        raise ValueError("recognized_challenges must be the supported set")


def _reject_widened_unknown(raw: object) -> None:
    if raw is None:
        return
    if not isinstance(raw, dict) or raw.get("modes") != ["public"]:
        raise ValueError("unknown domains are public-only")


def _reject_weakened_statuses(raw: object) -> None:
    if raw is None:
        return
    if not isinstance(raw, dict):
        raise ValueError("terminal_statuses must be a mapping")
    for key, value in raw.items():
        status = str(key)
        if status in BUILTIN_STATUS and str(value) != BUILTIN_STATUS[status]:
            raise ValueError(f"status {status} cannot escalate")


def _terminal_walls(raw: object) -> frozenset[str]:
    extra: set[str] = set()
    if raw is None:
        return REQUIRED_TERMINAL
    if not isinstance(raw, list):
        raise ValueError("terminal_walls must be a list")
    for item in raw:
        name = str(item).strip().lower()
        if name in RECOGNIZED_CHALLENGES:
            raise ValueError("recognized challenges are not terminal walls")
        if _WALL_NAME.fullmatch(name) is None:
            raise ValueError("terminal wall name")
        extra.add(name)
    return frozenset(REQUIRED_TERMINAL | extra)


def _domains(raw: object) -> tuple[DomainRule, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError("domains must be a list")
    rules: list[DomainRule] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("domain entry must be a mapping")
        rule = _domain(item)
        if rule.origin in seen:
            raise ValueError(f"duplicate origin {rule.origin}")
        seen.add(rule.origin)
        rules.append(rule)
    return tuple(rules)


def _domain(item: Mapping[str, object]) -> DomainRule:
    modes = _mode_list(item.get("modes"))
    challenges = _challenge_list(item.get("challenges"))
    if "solver" in modes and not challenges:
        raise ValueError("solver mode requires a recognized challenge list")
    if "zenrows" not in item or not isinstance(item.get("zenrows"), bool):
        raise ValueError("zenrows must be an explicit boolean")
    return DomainRule(
        origin=_canonical_origin(item.get("origin")),
        path_prefixes=_prefixes(item.get("path_prefixes")),
        resource_origins=_resources(item.get("resource_origins")),
        modes=modes,
        zenrows=bool(item["zenrows"]),
        challenges=challenges,
    )


def _mode_list(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, list) or not raw:
        raise ValueError("modes must be a non-empty list")
    modes: list[str] = []
    for item in raw:
        if item not in MODE_ORDER:
            raise ValueError("mode must be public, paid, or solver")
        if item not in modes:
            modes.append(str(item))
    return tuple(modes)


def _challenge_list(raw: object) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError("challenges must be a list")
    found: list[str] = []
    for item in raw:
        if item not in RECOGNIZED_CHALLENGES:
            raise ValueError("unsupported challenge")
        if item not in found:
            found.append(str(item))
    return tuple(found)


def _prefixes(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, list) or not raw:
        raise ValueError("path_prefixes must be a non-empty list")
    prefixes: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.startswith("/") or _path_escapes(item):
            raise ValueError("path prefix")
        normalized = _normalize_path(item)
        stored = "/" if normalized == "/" else normalized.rstrip("/") + "/"
        if stored not in prefixes:
            prefixes.append(stored)
    return tuple(prefixes)


def _resources(raw: object) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError("resource_origins must be a list")
    found: list[str] = []
    for item in raw:
        origin = _canonical_resource(item)
        if origin not in found:
            found.append(origin)
    return tuple(found)


def _budgets(raw: object) -> dict[str, object]:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("budgets must be a mapping")
    unknown = set(raw) - _BUDGET_KEYS
    if unknown:
        raise ValueError(f"unknown budget keys: {sorted(unknown)}")
    tariffs_raw = raw.get("tariffs") or {}
    providers_raw = raw.get("providers") or {}
    if not isinstance(tariffs_raw, dict) or not isinstance(providers_raw, dict):
        raise ValueError("tariffs and providers must be mappings")
    return {
        "daily_micro_usd": _budget_int(raw.get("daily_micro_usd", 0), "daily_micro_usd"),
        "monthly_micro_usd": _budget_int(raw.get("monthly_micro_usd", 0), "monthly_micro_usd"),
        "daily_retrieval_count": _budget_int(raw.get("daily_retrieval_count", 0), "daily_retrieval_count"),
        "daily_solver_count": _budget_int(raw.get("daily_solver_count", 0), "daily_solver_count"),
        "monthly_solver_count": _budget_int(raw.get("monthly_solver_count", 0), "monthly_solver_count"),
        "tariffs": {str(name): _budget_int(value, f"tariffs.{name}") for name, value in tariffs_raw.items()},
        "providers": {str(name): _provider_budget(name, value) for name, value in providers_raw.items()},
    }


def _provider_budget(name: object, raw: object) -> dict[str, int]:
    if not isinstance(name, str) or not name:
        raise ValueError("provider name")
    if not isinstance(raw, dict):
        raise ValueError(f"provider {name} caps must be a mapping")
    unknown = set(raw) - _PROVIDER_KEYS
    if unknown:
        raise ValueError(f"unknown provider cap: {sorted(unknown)}")
    return {key: _budget_int(value, f"{name}.{key}") for key, value in raw.items()}


def _budget_int(value: object, name: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"budget {name} must be a non-negative integer")
    return value


def _canonical_origin(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("origin must be an http(s) URL")
    parts = urlsplit(value)
    if parts.path not in {"", "/"} or parts.query or parts.fragment:
        raise ValueError("domain origin must be scheme and host only")
    parsed = _parse_url(value if parts.path else value + "/")
    if parsed is None:
        raise ValueError("domain origin is not a public http(s) origin")
    return parsed[0]


def _canonical_resource(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("resource origin")
    parts = urlsplit(value)
    if parts.query or parts.fragment or parts.username is not None or parts.password is not None:
        raise ValueError("resource origin must not carry credentials or a query")
    parsed = _parse_url(value if parts.path else value + "/")
    if parsed is None:
        raise ValueError("resource origin is blocked")
    origin = parsed[0]
    if not parts.path or parts.path == "/":
        return origin
    path = _normalize_path(parts.path).rstrip("/")
    return origin + path


def _on_path(rule: DomainRule, private: bool, wall: str | None) -> AccessDecision:
    challenge_ok = wall in rule.challenges and wall in RECOGNIZED_CHALLENGES
    paid = "paid" in rule.modes and rule.zenrows and not private and (wall is None or challenge_ok)
    solver = challenge_ok and "solver" in rule.modes and not private
    modes: set[str] = set()
    if "public" in rule.modes:
        modes.add("public")
    if paid:
        modes.add("paid")
    if solver:
        modes.add("solver")
    ordered = _ordered(modes)
    reason = _on_path_reason(private, wall, paid, solver, ordered, rule.zenrows)
    origins = rule.resource_origins if paid or solver else ()
    return AccessDecision(ordered, reason, origins, rule.path_prefixes)


def _on_path_reason(
    private: bool,
    wall: str | None,
    paid: bool,
    solver: bool,
    modes: tuple[str, ...],
    zenrows: bool,
) -> str | None:
    if private:
        return "private_topic"
    if wall is None:
        if modes:
            return None
        if not zenrows:
            return "zenrows_disabled"
        return "no_available_mode"
    if wall in RECOGNIZED_CHALLENGES:
        if paid or solver:
            return None
        return "challenge_not_authorized"
    return wall


def _off_path(rule: DomainRule, private: bool, wall: str | None) -> AccessDecision:
    modes = ("public",) if "public" in rule.modes else ()
    if private:
        reason: str | None = "private_topic"
    elif wall is None or wall in RECOGNIZED_CHALLENGES:
        reason = "path_not_allowlisted"
    else:
        reason = wall
    if not modes and reason is None:
        reason = "path_not_allowlisted"
    return AccessDecision(modes, reason, (), rule.path_prefixes)


def _unknown_reason(private: bool, wall: str | None) -> str | None:
    if private:
        return "private_topic"
    if wall is None:
        return None
    if wall in RECOGNIZED_CHALLENGES:
        return "challenge_not_authorized"
    return wall


def _ordered(modes: set[str]) -> tuple[str, ...]:
    return tuple(mode for mode in MODE_ORDER if mode in modes)


def _normalize_wall(wall_kind: str | int | None) -> str | None:
    if wall_kind is None:
        return None
    if isinstance(wall_kind, bool) or (not isinstance(wall_kind, (str, int))):
        return "unsupported_challenge"
    text = str(wall_kind).strip().lower()
    if text in BUILTIN_STATUS:
        return BUILTIN_STATUS[text]
    return text


def _parse_url(url: object) -> tuple[str, str] | None:
    if not isinstance(url, str) or not url:
        return None
    if any(ch.isspace() or ch == "\\" or ord(ch) < 32 or ord(ch) == 127 for ch in url):
        return None
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"}:
        return None
    if parts.username is not None or parts.password is not None:
        return None
    host = parts.hostname
    if host is None:
        return None
    host = host.rstrip(".").lower()
    if not host or "%" in host:
        return None
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    try:
        explicit = parts.port
    except ValueError:
        return None
    port = explicit if explicit is not None else (443 if scheme == "https" else 80)
    if port not in {80, 443}:
        return None
    if _host_denied(host) or _path_escapes(parts.path):
        return None
    return _origin_key(scheme, host, port), _normalize_path(parts.path)


def _origin_key(scheme: str, host: str, port: int) -> str:
    default = 443 if scheme == "https" else 80
    shown = _format_host(host)
    if port == default:
        return f"{scheme}://{shown}"
    return f"{scheme}://{shown}:{port}"


def _format_host(host: str) -> str:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host
    if isinstance(ip, ipaddress.IPv6Address):
        return f"[{ip.compressed}]"
    return str(ip)


def _host_denied(host: str) -> bool:
    literal = _literal_ip(host)
    if literal == "invalid":
        return True
    if literal is not None:
        return _ip_denied(literal)
    return host in _LOCAL_NAMES or host.endswith(".localhost") or host.endswith(".local")


def _literal_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | str | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    if _DOTTED_IP.fullmatch(host) is None:
        return None
    try:
        return _dotted_ipv4(host)
    except ValueError:
        return "invalid"


def _dotted_ipv4(host: str) -> ipaddress.IPv4Address:
    parts = host.split(".")
    numbers = [_ipv4_piece(part) for part in parts]
    if len(numbers) == 1:
        packed = numbers[0]
        if packed > 0xFFFFFFFF:
            raise ValueError
        return ipaddress.IPv4Address(packed)
    if len(numbers) == 2 and numbers[0] <= 255 and numbers[1] <= 0xFFFFFF:
        return ipaddress.IPv4Address((numbers[0] << 24) | numbers[1])
    if len(numbers) == 3 and numbers[0] <= 255 and numbers[1] <= 255 and numbers[2] <= 0xFFFF:
        return ipaddress.IPv4Address((numbers[0] << 24) | (numbers[1] << 16) | numbers[2])
    if len(numbers) == 4 and all(number <= 255 for number in numbers):
        return ipaddress.IPv4Address((numbers[0] << 24) | (numbers[1] << 16) | (numbers[2] << 8) | numbers[3])
    raise ValueError


def _ipv4_piece(part: str) -> int:
    if part.startswith("0x"):
        if re.fullmatch(r"0x[0-9a-f]+", part) is None:
            raise ValueError
        return int(part, 16)
    if len(part) > 1 and part[0] == "0":
        if re.fullmatch(r"[0-7]+", part) is None:
            raise ValueError
        return int(part, 8)
    if not part.isdigit():
        raise ValueError
    return int(part, 10)


def _ip_denied(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None and _ip_denied(ip.ipv4_mapped):
        return True
    if any(ip in net for net in _DENY_NETS):
        return True
    return bool(ip.is_multicast or ip.is_loopback or ip.is_link_local or ip.is_unspecified)


def _path_escapes(path: str) -> bool:
    current = path
    for _ in range(3):
        if _has_dotdot(current) or _has_encoded_danger(current):
            return True
        decoded = unquote(current)
        if decoded == current:
            break
        current = decoded
    return _has_dotdot(current) or _has_encoded_danger(current)


def _has_encoded_danger(path: str) -> bool:
    lowered = path.lower()
    return "\\" in path or "\x00" in path or "%00" in lowered or "%5c" in lowered


def _has_dotdot(path: str) -> bool:
    for segment in path.split("/"):
        lowered = segment.lower()
        if lowered in {"..", "%2e%2e", "%2e.", ".%2e"} or unquote(segment) == "..":
            return True
    return False


def _normalize_path(path: str) -> str:
    decoded = path
    for _ in range(3):
        nxt = unquote(decoded)
        if nxt == decoded:
            break
        decoded = nxt
    pieces = [segment for segment in decoded.split("/") if segment not in {"", "."}]
    if not pieces:
        return "/"
    return "/" + "/".join(pieces)


def _prefix_match(path: str, prefixes: tuple[str, ...]) -> bool:
    for prefix in prefixes:
        if prefix == "/":
            return True
        base = prefix.rstrip("/")
        if path == base or path.startswith(base + "/"):
            return True
    return False


__all__ = [
    "MODE_ORDER",
    "RECOGNIZED_CHALLENGES",
    "REQUIRED_TERMINAL",
    "AccessDecision",
    "AccessPolicy",
    "AccessPolicyStale",
    "DomainRule",
    "load_access_policy",
]
