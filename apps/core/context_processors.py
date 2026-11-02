import json

from django.conf import settings

# Strict-CSP-friendly htmx (plan §6): no eval, no inline indicator styles, no scripts in
# swapped content, and no page HTML cached in localStorage (shared freezer tablets).
HTMX_CONFIG = {
    "allowEval": False,
    "allowScriptTags": False,
    "includeIndicatorStyles": False,
    "historyCacheSize": 0,
    "selfRequestsOnly": True,
}


def labstock(request):
    return {
        "site_url": settings.SITE_URL,
        "htmx_config": json.dumps(HTMX_CONFIG),
        "current_lab": getattr(request, "lab", None),
    }
