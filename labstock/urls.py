from allauth.account.decorators import secure_admin_login
from django.contrib import admin
from django.urls import include, path

from apps.core.views import healthz

# Admin logins go through allauth, so they get its rate limits and MFA (plan §12).
admin.site.login = secure_admin_login(admin.site.login)
admin.site.site_header = "LabStock admin"
admin.site.site_title = "LabStock"

urlpatterns = [
    path("healthz", healthz, name="healthz"),
    path("admin/", admin.site.urls),
    path("accounts/", include("allauth.urls")),
    path("quartzy/", include("apps.quartzy.urls")),
    path("guard/", include("apps.guard.urls")),
    path("reports/", include("apps.reports.urls")),
    path("", include("apps.core.urls")),
    path("", include("apps.bench.urls")),
]
