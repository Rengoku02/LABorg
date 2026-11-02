from allauth.account.adapter import DefaultAccountAdapter
from django.conf import settings

from .models import Membership


class LabAccountAdapter(DefaultAccountAdapter):
    """Closed signup; only current lab members (or superusers) may log in."""

    def is_open_for_signup(self, request):
        return False

    def get_from_email(self):
        return settings.LABSTOCK_ALERTS_FROM

    def pre_login(self, request, user, **kwargs):
        response = super().pre_login(request, user, **kwargs)
        if response is not None:
            return response
        if user.is_superuser:
            return None
        has_current = any(m.is_current() for m in Membership.objects.filter(user=user))
        if not has_current:
            from allauth.account.internal.stagekit import clear_login

            clear_login(request)
            return self.respond_user_inactive(request, user)
        return None
