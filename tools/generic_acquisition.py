"""Generic public acquisition: search and fetch for the generic research harness.

Ordinary public HTTP only. Reuses the verified safety policy instead of an unsafe
urlopen clone:

- SSRF: tools/egress_guard.validate_target on EVERY hop (scheme, credentials,
  local names, IP literals, port allowlist, all DNS answers public), then the
  transport pins that exact IP (CURLOPT_RESOLVE) so re-resolution cannot rebind.
  Redirects are followed manually and every redirect target is re-validated.
- robots.txt: the orchestrator parsing policy, checked per origin before dial.
- Walls: 401/403/407/451, challenge markers, and short login pages are refusals,
  reusing the orchestrator wall patterns.
- Rate: per-domain token bucket (orchestrator section 4.3 policy, burst 3).
- Untrusted source text: sanitizer cleanup plus quarantine scan; a poisoned page
  raises rather than returning injected instructions. Text is data, never tools.

Paid escalation is deliberately absent: this module performs ordinary public
HTTP only and never activates paid providers. Loopback is reachable only by an
operator-configured search service endpoint (SNOWBALL_SEARCH_URL, trusted
deployment infrastructure) or by explicitly injected test transports; the
production fetch path has no loopback exception.

No special domain semantics and no hardcoded domain knowledge live here.
Tests inject the resolver and transports; production code paths require no
network access until a caller invokes search/fetch.
"""
from __future__ import annotations

import contextlib
import ipaddress
import json
import re
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import egress_guard, quarantine, sanitizer  # noqa: E402

DEFAULT_MAX_BYTES = 5_242_880
DEFAULT_TIMEOUT_SEC = 30.0
DEFAULT_MAX_REDIRECTS = 5
DEFAULT_ROBOTS_MAX_BYTES = 65_536
DEFAULT_MAX_LINKS = 500
SEARCH_LIMIT_MAX = 10
SEARCH_MAX_CHARACTERS = 4_000
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
WALL_STATUS = frozenset({401, 403, 407, 451})

# Orchestrator wall/markup patterns (classify_response policy), kept in this
# module so importing acquisition never pulls the specialist orchestrator.
CHALLENGE_RX = re.compile(
    r"(cf-chl|__cf_chl_|challenge-platform|Just a moment\.\.\.|Attention Required!|cf-turnstile|"
    r"Please verify you are a human|hcaptcha\.com|recaptcha/api|g-recaptcha|px-captcha|perimeterx|"
    r"datadome|Access Denied.{0,40}akamai|Checking your browser|enable JavaScript and cookies to continue)",
    re.IGNORECASE,
)
LOGIN_RX = re.compile(r'<input[^>]+type="password"', re.IGNORECASE)


class AcquisitionError(Exception):
    """Typed acquisition refusal. Field/message only; never secret data."""

    def __init__(
        self,
        field: str,
        message: str,
        *,
        url: str | None = None,
        wall_kind: str | None = None,
        retry_after: int | None = None,
        reason: str | None = None,
        hits: tuple[str, ...] = (),
    ) -> None:
        super().__init__(f"{field}: {message}")
        self.field = field
        self.message = message
        self.url = url
        self.wall_kind = wall_kind
        self.retry_after = retry_after
        self.reason = reason
        self.hits = hits


@dataclass(frozen=True)
class TransportResponse:
    """Single non-redirect-following HTTP response handed back by a transport."""

    status: int
    headers: dict[str, str]
    body: bytes
    encoding: str | None = None
    truncated: bool = False


class CurlPinnedTransport:
    """Production transport: curl_cffi with DNS pinned to the validated IP.

    The caller (Acquisition.fetch) has already validated the target through
    egress_guard; this transport enforces that validation at dial time via
    CURLOPT_RESOLVE, forbids proxy overrides, disables redirect following, and
    bounds the streamed body.
    """

    def __init__(self, user_agent: str) -> None:
        self.user_agent = user_agent

    def request(self, url: str, *, max_bytes: int, timeout: float, pin_ip: str) -> TransportResponse:
        from curl_cffi import CurlOpt
        from curl_cffi import requests as cffi

        parts = urlsplit(url)
        port = parts.port or (443 if parts.scheme == "https" else 80)
        session = cffi.Session(impersonate="chrome")
        # Pin the connection to the exact validated address; forbid proxies that
        # would move the dial elsewhere.
        session.curl.setopt(CurlOpt.RESOLVE, [f"{parts.hostname}:{port}:{pin_ip}"])
        session.curl.setopt(CurlOpt.PROXY, "")
        try:
            response = session.get(
                url,
                allow_redirects=False,
                timeout=max(1.0, timeout),
                headers={"User-Agent": self.user_agent, "Accept-Language": "en-US,en;q=0.9"},
                stream=True,
            )
            buf = bytearray()
            truncated = False
            for chunk in response.iter_content():
                buf += chunk
                if len(buf) > max_bytes:
                    buf = buf[:max_bytes]
                    truncated = True
                    break
            # Read every attribute we need BEFORE close(): curl_cffi objects may
            # not serve stale properties once the underlying curl handle is done.
            status = int(response.status_code)
            headers = {str(k).lower(): str(v) for k, v in dict(response.headers).items()}
            encoding = response.encoding
            response.close()
            return TransportResponse(status, headers, bytes(buf), encoding, truncated)
        except Exception as exc:  # noqa: BLE001 - normalized for the caller
            if "timeout" in type(exc).__name__.lower():
                raise TimeoutError(str(exc)) from exc
            raise OSError(f"transport: {exc}") from exc
        finally:
            with contextlib.suppress(Exception):
                session.close()


class _SearchRedirectBlocker(urllib.request.HTTPRedirectHandler):
    """Search service responses are never followed across redirects."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ARG002
        return None


class SearchResponseError(AcquisitionError):
    """Malformed search-service output; normalized through search() as 'search'."""

    def __init__(self, detail: str) -> None:
        super().__init__("search", f"malformed search response: {detail}")


class _BoundedReader:
    """File-like bounded reader: byte cap + absolute monotonic deadline.

    Uses ``read1``-style chunked reads (falling back to bounded ``read``) and
    resets the underlying socket timeout to the REMAINING time before every
    blocking read, so a trickling body can never hold the read open past the
    deadline and no reader thread outlives the exchange.
    """

    def __init__(self, fp: Any, cap: int, end_by: float) -> None:
        self._fp = fp
        self._cap = cap
        self._end_by = end_by
        self._read_total = 0

    def _remaining(self) -> float:
        return self._end_by - time.monotonic()

    def _reset_timeout(self) -> None:
        """Clamp the socket timeout to the time left before the deadline."""
        remaining = self._remaining()
        if remaining <= 0:
            raise TimeoutError("search response exceeded its deadline")
        fp = getattr(self._fp, "fp", None) or getattr(getattr(self._fp, "_fp", None), "fp", None)
        raw_sock = getattr(fp, "raw", None) if fp is not None else None
        socket_obj = getattr(fp, "_sock", None)
        timeout = max(0.05, min(remaining, 30.0))
        for candidate in (raw_sock, socket_obj, getattr(self._fp, "_sock", None)):
            sock = getattr(candidate, "_sock", None) or candidate
            with contextlib.suppress(Exception):
                if hasattr(sock, "settimeout"):
                    sock.settimeout(timeout)

    def read(self, amount: int = -1) -> bytes:
        if self._remaining() <= 0:
            raise TimeoutError("search response exceeded its deadline")
        parts: list[bytes] = []
        while True:
            wanted = self._cap - self._read_total
            if amount >= 0:
                wanted = min(wanted, amount - sum(len(p) for p in parts))
            if wanted <= 0:
                break
            self._reset_timeout()
            chunk = b""
            if hasattr(self._fp, "read1"):
                chunk = self._fp.read1(wanted)
            else:
                chunk = self._fp.read(wanted)
            if not chunk:
                break
            parts.append(chunk)
            self._read_total += len(chunk)
            if self._read_total >= self._cap:
                raise OSError(f"search response exceeds {self._cap} bytes")
        return b"".join(parts)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._fp, name)


def read_search_payload(fp: Any, cap: int, end_by: float) -> bytes:
    """Bounded, deadline-enforcing read; typed failures, always closes.

    Returns the payload bytes. Raises TimeoutError on deadline expiry,
    OSError('search response exceeds ...') on cap breach; the underlying file
    object is closed in ``finally`` either way.
    """
    reader = _BoundedReader(fp, cap, end_by)
    try:
        return reader.read()
    finally:
        with contextlib.suppress(Exception):
            fp.close()


def default_search_transport(
    url: str, body: bytes, *, timeout: float, headers: dict[str, str]
) -> tuple[int, dict[str, str], bytes]:
    """POST to the operator-configured search service.

    Env proxies are removed, redirects are refused (credentials must never be
    forwarded to a redirect target), reads are byte-bounded chunked reads with
    the socket timeout re-armed to the remaining time before every blocking
    read, and an absolute deadline covers the whole exchange. Deadline/oversize
    violations raise typed errors, never truncate silently.
    """
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _SearchRedirectBlocker
    )
    request = urllib.request.Request(
        url, data=body, method="POST", headers={"Content-Type": "application/json", **headers}
    )
    end_by = time.monotonic() + max(timeout, 1.0)
    cap = max(1_048_576, SEARCH_LIMIT_MAX * (SEARCH_MAX_CHARACTERS * 8 + 4096))
    try:
        with opener.open(request, timeout=max(timeout, 1.0)) as response:
            payload = read_search_payload(response, cap, end_by)
            return (
                int(response.status),
                {str(k).lower(): str(v) for k, v in response.headers.items()},
                payload,
            )
    except urllib.error.HTTPError as exc:
        # Read the error body under the same bounds. Any read failure (deadline,
        # cap, socket error) propagates as a typed error instead of being
        # suppressed into an unbound payload; the error body file is closed in
        # read_search_payload's finally either way.
        payload = b""
        status = int(exc.code)
        headers_low = {str(k).lower(): str(v) for k, v in exc.headers.items()} if exc.headers else {}
        try:
            payload = read_search_payload(exc, cap, end_by)
        except TimeoutError as read_exc:
            raise TimeoutError(f"search error-body read failed: {read_exc}") from read_exc
        except OSError as read_exc:
            if "exceeds" in str(read_exc):
                raise AcquisitionError("oversize", str(read_exc)) from read_exc
            raise OSError(f"search error-body read failed: {read_exc}") from read_exc
        return status, headers_low, payload
    except urllib.error.URLError as exc:
        if "timed out" in str(exc.reason).lower():
            raise TimeoutError(str(exc)) from exc
        raise OSError(f"search service: {exc}") from exc
    except TimeoutError:
        raise
    except OSError as exc:
        if "exceeds" in str(exc):
            raise AcquisitionError("oversize", str(exc)) from exc
        raise


class DomainBuckets:
    """Per-domain token bucket (orchestrator section 4.3: burst 3, N/min).

    clock/sleeper are injectable so tests drive waits deterministically.
    """

    def __init__(
        self,
        per_min: int = 6,
        burst: int = 3,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.rate = max(per_min, 1) / 60.0
        self.burst = max(burst, 1)
        self.state: dict[str, tuple[float, float]] = {}
        self.lock = threading.Lock()
        self.clock = clock
        self.sleeper = sleeper

    def wait(self, domain: str, *, deadline: float | None = None) -> None:
        """Wait for a token, bounded by the absolute acquisition ``deadline``.

        Raises ``AcquisitionError('timeout', ...)`` — never a raw exception —
        when the deadline would expire during the wait. Sleeps are clipped to
        the remaining time so the wait always terminates by the deadline.
        """
        while True:
            with self.lock:
                tokens, ts = self.state.get(domain, (float(self.burst), self.clock()))
                tokens = min(float(self.burst), tokens + (self.clock() - ts) * self.rate)
                if tokens >= 1:
                    self.state[domain] = (tokens - 1, self.clock())
                    return
                self.state[domain] = (tokens, self.clock())
                sleep = min((1 - tokens) / self.rate, 15.0)
                if deadline is not None:
                    remaining = deadline - self.clock()
                    if remaining <= 0:
                        raise AcquisitionError("timeout", "deadline reached during rate-limit wait")
                    sleep = min(sleep, remaining)
            self.sleeper(sleep)


def norm_text(s: str) -> str:
    """Orchestrator normalization: NFKC, straight quotes/dashes, collapsed space."""
    s = unicodedata.normalize("NFKC", s)
    s = s.translate(str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-", "\u00a0": " "}))
    return re.sub(r"\s+", " ", s).strip()


def canonical_url(raw: str) -> str | None:
    """Execution-safe canonical URL used for authorization AND provenance.

    Safe normalization only: hostname case folding + IDNA, default-port
    removal, and fragment removal. Scheme, hostname (no www/old/mobile
    folding), path (no index-file stripping or slash trimming), and query
    (no tracking-param removal or reordering) are preserved exactly, so an
    approved seed can never silently authorize or attribute a different
    resource. Returns None for non-http(s), missing host, or unparseable
    input.
    """
    try:
        parts = urlsplit(str(raw).strip())
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return None
        host = parts.hostname.encode("idna").decode().lower().rstrip(".")
        # Access .port inside the try: a malformed port (out of range, or
        # non-numeric) raises ValueError on extraction and must yield None.
        port = parts.port
    except (ValueError, UnicodeError):
        return None
    if port == 0:
        # Port 0 is not a connectable http(s) origin; treat as malformed.
        return None
    if port is not None and port == (443 if parts.scheme == "https" else 80):
        port = None  # default port is implied by the scheme
    netloc = host if port is None else f"{host}:{port}"
    return urlunsplit((parts.scheme, netloc, parts.path or "/", parts.query, ""))


class _Stripper(HTMLParser):
    """Readable-text stripper (orchestrator parse_html policy)."""

    SKIP = {"script", "style", "noscript", "svg", "template", "nav", "footer", "header"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.links: list[str] = []
        self.title = ""
        self._skip = 0
        self._in_title = False
        self._href: str | None = None
        self._anchor: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.SKIP:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag == "a":
            self._href = dict(attrs).get("href")
            self._anchor = []
        if tag in ("p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "section", "article"):
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False
        elif tag == "a" and self._href is not None:
            self.links.append(self._href)
            self._href = None

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        if self._skip:
            return
        self.parts.append(data)
        if self._href is not None:
            self._anchor.append(data)


# Spec 3.2 prefers trafilatura for hash stability when it is installed. Tests
# that need the deterministic stdlib stripper flip this flag.
_PREFER_TRAFILATURA = True


def parse_html(html: str) -> tuple[str, str, list[str]]:
    """(title, main text, hrefs). trafilatura when installed, stdlib stripper otherwise."""
    parser = _Stripper()
    with contextlib.suppress(Exception):
        parser.feed(html)
    text = "\n".join(norm_text(line) for line in "".join(parser.parts).split("\n") if line.strip())
    if _PREFER_TRAFILATURA:
        try:
            import trafilatura

            extracted = trafilatura.extract(html, include_links=False, include_comments=False)
            if extracted:
                text = extracted
        except ImportError:
            pass
    return norm_text(parser.title)[:300], text, parser.links


def classify_status(status: int, raw_html: str) -> tuple[str, str | None]:
    """(outcome, wall_kind): ok | walled | rate_limited | outage | http_error."""
    if status in WALL_STATUS:
        return "walled", "http"
    if status == 200 and CHALLENGE_RX.search(raw_html[:20000]):
        return "walled", "challenge"
    if status == 429:
        return "rate_limited", None
    if status in (500, 502, 503, 504):
        return "outage", None
    if status >= 400:
        return "http_error", None
    return "ok", None


def _charset(content_type: str | None) -> str | None:
    if not content_type or "charset=" not in content_type:
        return None
    return content_type.split("charset=", 1)[1].split(";", 1)[0].strip().strip('"') or None


def _retry_after(headers: dict[str, str]) -> int | None:
    value = headers.get("retry-after", "")
    return int(value) if value.isdigit() else None


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise AcquisitionError(field, f"{field} must be a positive integer")
    return value


class Acquisition:
    """Public search + fetch with the shared pinned/robots/walls/rate policy."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = dict(config or {})
        self._timeout = float(cfg.get("timeout", DEFAULT_TIMEOUT_SEC))
        self._max_redirects = int(cfg.get("max_redirects", DEFAULT_MAX_REDIRECTS))
        self._robots_max_bytes = int(cfg.get("robots_max_bytes", DEFAULT_ROBOTS_MAX_BYTES))
        self._max_links = int(cfg.get("max_links", DEFAULT_MAX_LINKS))
        self._user_agent = str(cfg.get("user_agent", "SnowballResearchAgent/1.0 (public research harness)"))
        self._resolver: Callable[[str, int], list[str]] = cfg.get("resolver") or egress_guard.default_resolver
        self._transport = cfg.get("transport") or CurlPinnedTransport(self._user_agent)
        clock = cfg.get("clock") or time.monotonic
        sleeper = cfg.get("sleeper") or time.sleep
        self._buckets = DomainBuckets(
            int(cfg.get("domain_rate_per_min", 6)),
            int(cfg.get("domain_burst", 3)),
            clock=clock,
            sleeper=sleeper,
        )
        self._clock = clock
        # Search service: explicitly operator-configured. No default endpoint.
        self._search_url = str(cfg.get("search_url") or "").strip() or str(
            __import__("os").environ.get("SNOWBALL_SEARCH_URL", "")
        ).strip()
        self._search_api_key = str(cfg.get("search_api_key") or "").strip() or str(
            __import__("os").environ.get("SNOWBALL_SEARCH_API_KEY", "")
        ).strip()
        self._search_timeout = float(cfg.get("search_timeout", 60.0))
        self._search_max_chars = int(cfg.get("search_max_characters", SEARCH_MAX_CHARACTERS))
        self._search_transport = cfg.get("search_transport") or default_search_transport
        self._robots_rules_cache: dict[str, tuple[str, ...] | None] = {}

    # ---- search ----

    def search(self, query: str, *, limit: int = 5) -> list[dict[str, Any]]:
        """Search through the configured service endpoint. Results are untrusted."""
        if not isinstance(query, str) or not query.strip():
            raise AcquisitionError("query", "query required")
        limit = max(1, min(int(limit), SEARCH_LIMIT_MAX))
        if not self._search_url:
            raise AcquisitionError(
                "search_unavailable",
                "no search service configured; set SNOWBALL_SEARCH_URL or pass config['search_url']",
            )
        parts = urlsplit(self._search_url)
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
            raise AcquisitionError("search_unavailable", "SNOWBALL_SEARCH_URL must be a plain http(s) endpoint")
        endpoint = self._search_url if parts.path.rstrip("/").endswith("/search") else self._search_url.rstrip("/") + "/search"
        body = json.dumps(
            {
                "query": query.strip(),
                "numResults": limit,
                "contents": {"text": {"maxCharacters": self._search_max_chars}},
            }
        ).encode()
        headers = {"Accept": "application/json"}
        if self._search_api_key:
            headers["Authorization"] = f"Bearer {self._search_api_key}"
        try:
            status, headers, payload = self._search_transport(
                endpoint, body, timeout=self._search_timeout, headers=headers
            )
        except SearchResponseError:
            raise
        except AcquisitionError as exc:
            raise AcquisitionError(
                "search_unavailable" if exc.field in {"oversize", "timeout"} else exc.field, str(exc)
            ) from exc
        except TimeoutError as exc:
            raise AcquisitionError("search_unavailable", f"search service timeout: {exc}") from exc
        except (OSError, ConnectionError) as exc:
            raise AcquisitionError("search_unavailable", f"search service unreachable: {exc}") from exc
        status = int(status)
        headers = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
        if status == 429:
            raise AcquisitionError("search", "rate limited by the search service", retry_after=_retry_after(headers))
        if status != 200:
            raise AcquisitionError("search", f"status {status}")
        # Bound the received payload at the consumer too: the bounding also holds
        # for injected custom transports, not only the default one.
        payload_cap = max(1_048_576, SEARCH_LIMIT_MAX * (SEARCH_MAX_CHARACTERS * 8 + 4096))
        if len(payload or b"") > payload_cap:
            raise AcquisitionError(
                "oversize", f"search response exceeds {payload_cap} bytes"
            )
        try:
            data = json.loads(payload.decode("utf-8", "replace"))
        except (ValueError, UnicodeError) as exc:
            raise AcquisitionError("search", "malformed response from the search service") from exc
        if isinstance(data, list):
            rows = data  # some rotator deployments return a bare result array
        elif isinstance(data, dict) and isinstance(data.get("results"), list):
            rows = data["results"]
        else:
            raise AcquisitionError("search", "malformed response from the search service")
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        # Structural malformation is an explicit typed error (never silently
        # false-empty); policy-rejected URLs (non-http, credentials) are skipped
        # as unsafe rows while structurally valid rows still fill the batch.
        for row in rows:
            if len(out) >= limit:
                break
            if not isinstance(row, dict):
                raise SearchResponseError("result row is not an object")
            raw_url = row.get("url")
            if not isinstance(raw_url, str) or not raw_url.strip():
                raise SearchResponseError("result row has no url")
            if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in raw_url):
                raise SearchResponseError("result url carries control characters")
            try:
                up = urlsplit(raw_url.strip())
                canon = canonical_url(raw_url)
            except ValueError as exc:
                raise SearchResponseError("result url does not parse") from exc
            if up.scheme not in ("http", "https") or up.username or up.password:
                continue  # unsafe URL class: skip this row, keep the batch
            if not canon:
                raise SearchResponseError("result url does not canonicalize")
            if canon in seen:
                continue
            title = sanitizer.sanitize_untrusted_text(str(row.get("title") or ""))[:300]
            text = sanitizer.sanitize_untrusted_text(str(row.get("text") or ""))[: self._search_max_chars]
            if quarantine.scan_text(title + "\n" + text):
                continue  # quarantined poisoned hit, not returned as usable content
            seen.add(canon)
            out.append({"url": canon, "title": title, "text": text, "provenance": "search_result"})
        if not out:
            # A nonempty response batch that yields no structurally valid hit is a
            # malformed batch, not a genuine empty result (never false-empty).
            if rows:
                raise SearchResponseError("nonempty result batch had no valid rows")
        return out

    # ---- fetch ----

    def fetch(self, url: str, *, max_bytes: int = DEFAULT_MAX_BYTES) -> dict[str, Any]:
        """Fetch one public page through the pinned guarded path."""
        if not isinstance(url, str) or not url.strip():
            raise AcquisitionError("url", "url required")
        max_bytes = _positive_int(max_bytes, "max_bytes")
        started = self._clock()
        waited: set[str] = set()
        current = url.strip()
        final: tuple[Any, dict[str, str], int, str] | None = None
        for hop in range(self._max_redirects + 1):
            try:
                target = egress_guard.validate_target(current, resolver=self._resolver)
            except egress_guard.EgressDenied as exc:
                raise AcquisitionError(
                    "ssrf", f"egress denied: {exc.reason}", url=current, reason=exc.reason
                ) from exc
            parts = urlsplit(current)
            scheme_host = f"{parts.scheme}://{parts.netloc}"
            if scheme_host not in self._robots_rules_cache:
                self._robots_rules_cache[scheme_host] = self._robots_rules(scheme_host, started)
            if not self.robots_path_allowed(scheme_host, parts.path or "/"):
                raise AcquisitionError("robots", "disallowed by robots.txt", url=current)
            if target.host not in waited:
                self._buckets.wait(target.host, deadline=started + self._timeout)
                waited.add(target.host)
            remaining = self._timeout - (self._clock() - started)
            if remaining <= 0:
                raise AcquisitionError("timeout", "deadline reached", url=current)
            try:
                resp = self._transport.request(
                    target.url, max_bytes=max_bytes, timeout=remaining, pin_ip=target.ip
                )
            except TimeoutError as exc:
                raise AcquisitionError("timeout", str(exc), url=current) from exc
            except (OSError, ConnectionError) as exc:
                raise AcquisitionError("outage", f"transport: {exc}", url=current) from exc
            headers = {str(k).lower(): str(v) for k, v in (resp.headers or {}).items()}
            status = int(resp.status)
            if status in REDIRECT_STATUSES:
                location = headers.get("location")
                if not location:
                    raise AcquisitionError("http_error", f"status {status} without location", url=current)
                if hop >= self._max_redirects:
                    raise AcquisitionError(
                        "too_many_redirects", f"more than {self._max_redirects} redirects", url=current
                    )
                current = urljoin(current, location)
                continue
            final = (resp, headers, status, current)
            break
        if final is None:
            raise AcquisitionError("too_many_redirects", f"more than {self._max_redirects} redirects", url=current)
        resp, headers, status, current = final
        body = resp.body or b""
        if len(body) > max_bytes or getattr(resp, "truncated", False):
            raise AcquisitionError("oversize", f"body exceeds {max_bytes} bytes", url=current)
        encoding = resp.encoding or _charset(headers.get("content-type")) or "utf-8"
        raw_html = body.decode(encoding, "replace")
        outcome, wall = classify_status(status, raw_html)
        if outcome == "walled":
            raise AcquisitionError("walled", f"status {status}", url=current, wall_kind=wall)
        if outcome == "rate_limited":
            raise AcquisitionError("rate_limited", "status 429", url=current, retry_after=_retry_after(headers))
        if outcome == "outage":
            raise AcquisitionError("outage", f"status {status}", url=current)
        if outcome == "http_error":
            raise AcquisitionError("http_error", f"status {status}", url=current)
        title, text, hrefs = parse_html(raw_html)
        if LOGIN_RX.search(raw_html) and len(text) < 600:
            raise AcquisitionError("walled", "short login page", url=current, wall_kind="login")
        if len(text) < 200:
            raise AcquisitionError("empty_text", "rendered text under 200 characters", url=current)
        text = sanitizer.sanitize_untrusted_text(text)
        title = sanitizer.sanitize_untrusted_text(title)
        hits = quarantine.scan_text(title + "\n" + text)
        if hits:
            raise AcquisitionError(
                "injection",
                "untrusted source quarantined: " + ", ".join(hits),
                url=current,
                hits=tuple(hits),
            )
        canon = canonical_url(current) or current
        links: list[str] = []
        seen: set[str] = set()
        for href in hrefs:
            if len(links) >= self._max_links:
                break
            if not href:
                continue
            try:
                absolute = urljoin(current, href)
                parts = urlsplit(absolute)
            except ValueError:
                continue  # malformed untrusted link: skip, never abort the source
            if parts.scheme not in ("https", "http") or parts.username or parts.password:
                continue
            link_canon = canonical_url(absolute)
            if not link_canon or link_canon in seen:
                continue
            seen.add(link_canon)
            links.append(link_canon)
        return {
            "url": canon,
            "title": title,
            "text": text,
            "links": links,
            "provenance": "public_fetch",
            "source_kind": "public_fetch",
            "bytes": len(body),
            "content_type": headers.get("content-type", "").split(";")[0].strip().lower() or None,
            "raw_bytes": body,
        }

    # ---- robots (orchestrator parsing policy, per origin) ----

    def _robots_rules(self, scheme_host: str, started: float) -> tuple[str, ...] | None:
        """Fetch + parse robots.txt disallow rules for an origin once.

        Returns the tuple of applicable ``Disallow`` prefixes, or None when no
        robots file exists (404). The RULES are cached per origin; the caller
        evaluates each requested page path against them, never a cached verdict.
        Any refusal condition raises: 401/403/407/451 -> 'robots'; 429 ->
        'rate_limited' with retry_after; 5xx/other transport failure -> 'outage';
        redirect into denied target -> 'ssrf'; truncated/oversized body ->
        'oversize' (an incomplete policy is never treated as permission).
        """
        robots_url = f"{scheme_host}/robots.txt"
        try:
            target = egress_guard.validate_target(robots_url, resolver=self._resolver)
        except egress_guard.EgressDenied as exc:
            raise AcquisitionError(
                "ssrf", f"egress denied: {exc.reason}", url=robots_url, reason=exc.reason
            ) from exc
        remaining = self._timeout - (self._clock() - started)
        if remaining <= 0:
            raise AcquisitionError("timeout", "deadline reached", url=robots_url)
        try:
            resp = self._transport.request(
                target.url,
                max_bytes=self._robots_max_bytes,
                timeout=min(remaining, self._timeout),
                pin_ip=target.ip,
            )
        except TimeoutError as exc:
            # Explicit transport timeout on robots: conservative outage, no page dial.
            raise AcquisitionError("outage", f"robots unreachable: {exc}", url=robots_url) from exc
        except (OSError, ConnectionError) as exc:
            # Transport outage on robots: conservative refusal, no page dial.
            raise AcquisitionError("outage", f"robots unreachable: {exc}", url=robots_url) from exc
        # A truncated robots policy is an incomplete permission document: reject
        # before treating any parsed prefix as permission.
        if getattr(resp, "truncated", False) or len(resp.body or b"") > self._robots_max_bytes:
            raise AcquisitionError(
                "oversize", f"robots body exceeds {self._robots_max_bytes} bytes", url=robots_url
            )
        status = int(resp.status)
        if status in REDIRECT_STATUSES:
            # A redirecting robots.txt: follow exactly one hop, re-validated, then
            # treat the next answer as final (no unbounded robots redirect chains).
            headers_low = {str(k).lower(): str(v) for k, v in dict(resp.headers or {}).items()}
            location = headers_low.get("location", "") or ""
            if not location:
                # Redirect without Location: nothing to follow; ambiguous -> refuse.
                raise AcquisitionError("http_error", "robots redirect without location", url=robots_url)
            redirected = urljoin(target.url, location)
            try:
                second = egress_guard.validate_target(redirected, resolver=self._resolver)
                resp = self._transport.request(
                    second.url,
                    max_bytes=self._robots_max_bytes,
                    timeout=min(self._timeout, max(1.0, self._timeout - (self._clock() - started))),
                    pin_ip=second.ip,
                )
            except egress_guard.EgressDenied as exc:
                raise AcquisitionError(
                    "ssrf", f"egress denied: {exc.reason}", url=redirected, reason=exc.reason
                ) from exc
            except TimeoutError as exc:
                raise AcquisitionError("outage", f"robots unreachable: {exc}", url=redirected) from exc
            except (OSError, ConnectionError) as exc:
                raise AcquisitionError("outage", f"robots unreachable: {exc}", url=redirected) from exc
            if getattr(resp, "truncated", False) or len(resp.body or b"") > self._robots_max_bytes:
                raise AcquisitionError(
                    "oversize", f"robots body exceeds {self._robots_max_bytes} bytes", url=redirected
                )
            status = int(resp.status)
            if status in REDIRECT_STATUSES:
                raise AcquisitionError(
                    "too_many_redirects", "robots redirect chain", url=redirected
                )
        if status == 404:
            return None  # absent file: nothing is disallowed, nothing cached
        if status in WALL_STATUS:
            # 401/403/407/451: the site refuses robots inspection -> refuse the
            # whole origin, fail closed.
            raise AcquisitionError("robots", f"robots refused with status {status}", url=robots_url)
        if status == 429:
            resp_headers = {str(k).lower(): str(v) for k, v in dict(resp.headers or {}).items()}
            raise AcquisitionError(
                "rate_limited", "robots rate limited", url=robots_url, retry_after=_retry_after(resp_headers)
            )
        if status != 200:
            # 5xx or any other answer on robots: explicit conservative refusal —
            # access is unproven, the page fetch never executes.
            raise AcquisitionError("outage", f"robots status {status}", url=robots_url)
        txt = (resp.body or b"").decode(resp.encoding or "utf-8", "replace")
        agent_ok, disallow = False, []
        for line in txt.splitlines():
            line = line.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            key, value = (chunk.strip() for chunk in line.split(":", 1))
            if key.lower() == "user-agent":
                agent_ok = value == "*" or "snowball" in value.lower()
            elif key.lower() == "disallow" and agent_ok and value:
                disallow.append(value)
        return tuple(disallow)

    def robots_path_allowed(self, scheme_host: str, path: str) -> bool:
        """Evaluate the cached rule list for THIS path; never a cached verdict."""
        rules = self._robots_rules_cache.get(scheme_host)
        if rules is None:
            return True  # absent (404) or never fetched — nothing disallowed
        return not any(path.startswith(disallowed) for disallowed in rules)


def seed_check(url: str, approved: Any) -> tuple[bool, str]:
    """Gate helper for callers that approve URLs before dispatching fetch().

    ``approved`` may be a dict {canonical_url: {'label': ..., ...}} or a
    collection of canonical/absolute URL strings. Exact canonical-resource
    match required (safe normalization only: scheme/host/path/query kept;
    hostname case/IDNA/default-port/fragment normalized), except a root-path
    approval (path "/" or "") scopes to that exact scheme+host. Non-http(s)
    schemes, credentials, control characters, IP-literal hosts, non-default
    ports, and other resources are rejected with a stable reason string.
    """
    if isinstance(approved, dict):
        approved_keys = {str(key) for key in approved.keys()}
    else:
        try:
            approved_keys = {str(item) for item in approved}
        except TypeError:
            return False, "approved set must be a dict or a collection of URL strings"
    if not isinstance(url, str) or not url.strip():
        return False, "url required"
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in url):
        return False, "control characters in url"
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https"):
        return False, "scheme must be http(s)"
    if parts.username or parts.password:
        return False, "credentials in url are rejected"
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in parts.path + "?" + parts.query):
        return False, "control characters in url path or query"
    canon = canonical_url(url)
    if not canon:
        return False, "url does not canonicalize"
    page_host = (urlsplit(canon).hostname or "").lower()
    if not page_host:
        return False, "url has no host"
    try:
        ipaddress.ip_address(page_host)
    except ValueError:
        pass
    else:
        return False, "ip literal hosts are rejected"
    port = urlsplit(url.strip()).port
    if port is not None and port not in (80, 443):
        return False, f"port {port} is not an http(s) default"

    def _effective_port(scheme: str, host_port: int | None) -> int:
        return host_port if host_port is not None else (443 if scheme == "https" else 80)

    url_parts = urlsplit(canon)
    url_effective = _effective_port(url_parts.scheme, url_parts.port)
    for key in approved_keys:
        key_canon = canonical_url(key)
        if not key_canon:
            continue
        if key_canon == canon:
            return True, ""
        key_parts = urlsplit(key_canon)
        # Root-path approval scopes to the exact origin: scheme + host AND the
        # effective port (https://host:80 is a different origin than
        # https://host:443, even though the scheme matches).
        if (
            key_parts.scheme == url_parts.scheme
            and key_parts.hostname == url_parts.hostname
            and _effective_port(key_parts.scheme, key_parts.port) == url_effective
            and key_parts.path in ("", "/")
        ):
            return True, ""
    return False, "url is not an approved seed"

