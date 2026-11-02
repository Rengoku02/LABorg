"""Read-only client for Quartzy's public API (plan §8 "Client"). Pure Python: no Django.

The app mirrors Quartzy and must never write to it (plan §2), so the transport itself refuses
every method except GET and HEAD; nothing built on this client can send a write, whatever the
caller does. Methods return the JSON exactly as Quartzy sent it; ``schemas.py`` parses it.

Facts from docs/research/quartzy-public-api.yaml: list endpoints take only ``page`` plus a
filter or two and return bare JSON arrays; page base, page size, sort order and rate limits
are undocumented (scripts/quartzy_probe.py measures them); a missing id returns 404.
"""

import email.utils
import logging
import time
from datetime import UTC, datetime
from urllib.parse import quote

import httpx
from tenacity import Retrying, retry_if_exception, wait_exponential_jitter

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.quartzy.com"
AUTH_HEADER = "Access-Token"
USER_AGENT = "LabStock/0.1 (read-only mirror)"
SAFE_METHODS = frozenset({"GET", "HEAD"})
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
# Waiting longer than this inside one task would eat the worker's 600 s budget; the next sync
# cycle retries anyway, so a longer Retry-After ends the attempt (the error carries it).
MAX_RETRY_AFTER = 60.0
_BACKOFF = wait_exponential_jitter(initial=1, max=30, jitter=1)
_TRANSIENT = (httpx.TimeoutException, httpx.NetworkError)
_RATE_LIMIT_PREFIXES = ("x-ratelimit", "x-rate-limit", "ratelimit", "retry-after")


class QuartzyWriteBlocked(RuntimeError):
    """A request other than GET/HEAD was refused before it left the process."""


class QuartzyError(Exception):
    """Quartzy answered with an error, an unexpected body, or not at all (``status`` None)."""

    def __init__(self, message, *, status=None, body=None, retry_after=None):
        super().__init__(message)
        self.status = status
        self.body = body
        self.retry_after = retry_after


class QuartzyAuthError(QuartzyError):
    """401/403: the token is missing, revoked or lacks access. Never retried."""


class ReadOnlyTransport(httpx.BaseTransport):
    """Wraps another transport and refuses anything but GET/HEAD."""

    def __init__(self, transport):
        self._transport = transport

    def handle_request(self, request):
        if request.method.upper() not in SAFE_METHODS:
            raise QuartzyWriteBlocked(
                f"Quartzy is read-only for LabStock; refused {request.method} {request.url.path}"
            )
        return self._transport.handle_request(request)

    def close(self):
        self._transport.close()


def _retry_after(response):
    value = response.headers.get("Retry-After", "").strip()
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


def _is_retryable(exc):
    if isinstance(exc, _TRANSIENT):
        return True
    return isinstance(exc, QuartzyError) and exc.status in RETRY_STATUSES


def _segment(value):
    text = str(value).strip() if value is not None else ""
    if text in {"", ".", ".."}:
        raise ValueError(f"Not a usable Quartzy id: {value!r}")
    return quote(text, safe="")


class QuartzyClient:
    """Read-only Quartzy client.

    ``calls`` counts HTTP requests sent, retries included, so the sync can keep to its call
    budget. ``rate_limit_headers`` holds any rate-limit headers from the latest response
    (none are documented; the probe reports them if they appear). Retries cover 429/5xx and
    network timeouts with exponential backoff plus jitter, and never wait less than
    Retry-After. The token is never logged and stays out of ``repr``.
    """

    def __init__(
        self,
        token,
        *,
        base_url=DEFAULT_BASE_URL,
        timeout=20.0,
        transport=None,
        max_attempts=4,
        sleep=time.sleep,
        min_interval=0.0,
    ):
        token = (token or "").strip()
        # A newline or non-ASCII byte would make httpx/h11 raise an error that quotes the
        # header value, i.e. the token, so refuse it here with a message that doesn't.
        if not token or not token.isascii() or not token.isprintable():
            raise ValueError("The Quartzy access token is empty or contains invalid characters")
        self.base_url = base_url.rstrip("/")
        self.max_attempts = max(1, int(max_attempts))
        self.min_interval = float(min_interval)
        self.calls = 0
        self.rate_limit_headers = {}
        self._sleep = sleep
        self._last_sent = None
        self._http = httpx.Client(
            base_url=self.base_url,
            headers={AUTH_HEADER: token, "Accept": "application/json", "User-Agent": USER_AGENT},
            timeout=timeout,
            transport=ReadOnlyTransport(httpx.HTTPTransport() if transport is None else transport),
        )

    def __repr__(self):
        return f"QuartzyClient(base_url={self.base_url!r}, calls={self.calls})"

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()

    def close(self):
        self._http.close()

    # Endpoints -------------------------------------------------------------------------------

    def health(self):
        """GET /healthz (needs no token). Raises QuartzyError when Quartzy reports unhealthy."""
        return self._get("/healthz")

    def me(self):
        """GET /user: the token owner's {id, first_name, last_name, email}."""
        return self._get("/user", expect=dict)

    def labs(self, page=None, organization_id=None):
        return self._get("/labs", {"page": page, "organization_id": organization_id}, expect=list)

    def lab(self, lab_id):
        """GET /labs/{id}. A 404 raises: a lab we can't see is a configuration problem."""
        return self._get(f"/labs/{_segment(lab_id)}", expect=dict)

    def types(self, lab_id=None, page=None, name=None):
        return self._get("/types", {"lab_id": lab_id, "page": page, "name": name}, expect=list)

    def order_requests(self, lab_id=None, page=None):
        return self._get("/order-requests", {"lab_id": lab_id, "page": page}, expect=list)

    def order_request(self, request_id):
        """GET /order-requests/{id}, or None on 404 (deleted in Quartzy, plan §8)."""
        path = f"/order-requests/{_segment(request_id)}"
        return self._get(path, expect=dict, missing_ok=True)

    def inventory_items(self, lab_id=None, page=None):
        return self._get("/inventory-items", {"lab_id": lab_id, "page": page}, expect=list)

    def inventory_item(self, item_id):
        """GET /inventory-items/{id}, or None on 404."""
        path = f"/inventory-items/{_segment(item_id)}"
        return self._get(path, expect=dict, missing_ok=True)

    # Plumbing --------------------------------------------------------------------------------

    def _get(self, path, params=None, *, expect=None, missing_ok=False):
        query = {key: value for key, value in (params or {}).items() if value is not None}
        retrying = Retrying(
            stop=self._should_stop,
            wait=self._wait,
            retry=retry_if_exception(_is_retryable),
            sleep=self._sleep,
            before_sleep=self._log_retry,
            reraise=True,
        )
        try:
            response = retrying(self._send_once, path, query)
        except httpx.RequestError as exc:
            raise QuartzyError(f"GET {path} failed: {type(exc).__name__}") from exc
        if response.status_code == 404:
            if missing_ok:
                return None
            raise self._error(response, path)
        data = self._decode(response, path)
        if expect is not None and not isinstance(data, expect):
            wanted = "array" if expect is list else "object"
            raise QuartzyError(
                f"GET {path}: expected a JSON {wanted}, got {type(data).__name__}",
                status=response.status_code,
                body=self._scrub(response.text[:2000]),
            )
        return data

    def _send_once(self, path, query):
        self._pace()
        self.calls += 1
        response = self._http.get(path, params=query)
        self.rate_limit_headers = {
            name: value
            for name, value in response.headers.items()
            if name.lower().startswith(_RATE_LIMIT_PREFIXES)
        }
        if response.is_success or response.status_code == 404:
            return response
        raise self._error(response, path)

    def _pace(self):
        if self.min_interval > 0 and self._last_sent is not None:
            remaining = self.min_interval - (time.monotonic() - self._last_sent)
            if remaining > 0:
                self._sleep(remaining)
        self._last_sent = time.monotonic()

    def _error(self, response, path):
        status = response.status_code
        cls = QuartzyAuthError if status in (401, 403) else QuartzyError
        retry_after = _retry_after(response) if status in RETRY_STATUSES else None
        return cls(
            f"GET {path} returned {status}",
            status=status,
            body=self._scrub(response.text[:2000]),
            retry_after=retry_after,
        )

    def _decode(self, response, path):
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise QuartzyError(
                f"GET {path} returned a body that is not JSON",
                status=response.status_code,
                body=self._scrub(response.text[:2000]),
            ) from exc

    def _scrub(self, text):
        # Error bodies end up in logs and SyncError rows; make sure the token never does.
        return text.replace(self._http.headers[AUTH_HEADER], "[redacted]") if text else text

    def _should_stop(self, retry_state):
        if retry_state.attempt_number >= self.max_attempts:
            return True
        retry_after = getattr(retry_state.outcome.exception(), "retry_after", None)
        return retry_after is not None and retry_after > MAX_RETRY_AFTER

    def _wait(self, retry_state):
        retry_after = getattr(retry_state.outcome.exception(), "retry_after", None) or 0.0
        return max(_BACKOFF(retry_state), retry_after)

    def _log_retry(self, retry_state):
        exc = retry_state.outcome.exception()
        reason = f"status {exc.status}" if isinstance(exc, QuartzyError) else type(exc).__name__
        logger.warning(
            "Quartzy %s; retrying in %.1fs (attempt %d of %d)",
            reason,
            retry_state.next_action.sleep,
            retry_state.attempt_number + 1,
            self.max_attempts,
        )
