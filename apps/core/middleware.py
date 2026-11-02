from urllib.parse import urlencode

from django.conf import settings
from django.shortcuts import redirect
from django.urls import Resolver404, resolve, reverse

from .models import Lab, Membership


def resolve_lab(request):
    user = request.user
    if not user.is_authenticated:
        return None
    memberships = [
        m for m in Membership.objects.filter(user=user).select_related("lab") if m.is_current()
    ]
    wanted = request.session.get("lab_id")
    for membership in memberships:
        if membership.lab_id == wanted:
            return membership.lab
    if memberships:
        return memberships[0].lab
    if user.is_superuser:
        # The system owner may not be a lab member; with a single lab, use it.
        labs = list(Lab.objects.all()[:2])
        if len(labs) == 1:
            return labs[0]
    return None


class CurrentLabMiddleware:
    """Sets ``request.lab`` (or None). All tenant querysets are scoped to it."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.lab = resolve_lab(request)
        return self.get_response(request)


# URL names a staff user without an authenticator may still reach.
MFA_EXEMPT_URL_NAMES = {
    "mfa_activate_totp",
    "mfa_index",
    "mfa_reauthenticate",
    "mfa_generate_recovery_codes",
    "mfa_view_recovery_codes",
    "mfa_download_recovery_codes",
    "account_logout",
    "account_login",
    "account_reauthenticate",
    "account_confirm_login_code",
    "account_request_login_code",
    "healthz",
}


class StaffMFARequiredMiddleware:
    """Staff must set up TOTP before using anything else (plan §12 "Admin")."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if (
            settings.LABSTOCK_REQUIRE_STAFF_MFA
            and request.user.is_authenticated
            and request.user.is_staff
            and not request.path.startswith(settings.STATIC_URL)
            and not self._exempt(request)
            and not self._has_totp(request.user)
        ):
            target = reverse("mfa_activate_totp")
            return redirect(f"{target}?{urlencode({'next': request.get_full_path()})}")
        return self.get_response(request)

    @staticmethod
    def _exempt(request):
        try:
            match = resolve(request.path_info)
        except Resolver404:
            return False
        return match.url_name in MFA_EXEMPT_URL_NAMES

    @staticmethod
    def _has_totp(user):
        from allauth.mfa.models import Authenticator

        return Authenticator.objects.filter(user=user, type=Authenticator.Type.TOTP).exists()
