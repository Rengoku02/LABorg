"""Error tracking (plan §6 "Monitoring"): optional, through sentry-sdk to Sentry or a
self-hosted GlitchTip, only when LABSTOCK_SENTRY_DSN is set (labstock/settings/prod.py).

``scrub_event`` runs on every event before it leaves the server: it drops cookies, auth
headers, query strings and request bodies, and redacts signed-link tokens and search text
anywhere in the event. Personal data is never sent (``send_default_pii=False``).
"""

import re

_SECRETS = re.compile(
    r"(/a/|/claims/t/)[^/\s?#\"']+"  # signed email links
    r"|(/s\?)[^\s\"']+"  # what someone searched for
    r"|(Access-Token[\"']?\s*[:=]\s*[\"']?)[^\s\"',}]+",  # a Quartzy token in a message
    re.IGNORECASE,
)
_DROP_HEADERS = {"cookie", "authorization", "access-token", "x-csrftoken", "referer"}


def _redact(text):
    def replace(match):
        prefix = next(group for group in match.groups() if group)
        return f"{prefix}[redacted]"

    return _SECRETS.sub(replace, text)


def _walk(value):
    if isinstance(value, str):
        return _redact(value)
    if isinstance(value, dict):
        return {key: _walk(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_walk(item) for item in value]
    return value


def scrub_event(event, hint=None):
    request = event.get("request") or {}
    if request:
        request.pop("cookies", None)
        request.pop("data", None)
        request["query_string"] = ""
        if "url" in request:
            request["url"] = request["url"].split("?", 1)[0]
        headers = request.get("headers") or {}
        request["headers"] = {
            name: value for name, value in headers.items() if name.lower() not in _DROP_HEADERS
        }
        event["request"] = request
    event.pop("user", None)
    return _walk(event)


def init_error_tracking(dsn, *, environment="production", release=None):
    """Start sentry-sdk if a DSN is configured; returns whether it started."""
    if not dsn:
        return False
    import sentry_sdk
    from sentry_sdk.integrations.django import DjangoIntegration

    sentry_sdk.init(
        dsn=dsn,
        integrations=[DjangoIntegration()],
        send_default_pii=False,
        traces_sample_rate=0.0,
        environment=environment,
        release=release,
        before_send=scrub_event,
    )
    return True
