from django.urls import path

from . import views

app_name = "core"

urlpatterns = [
    path("", views.home, name="home"),
    path("a/<str:token>/<slug:action>/", views.action, name="action"),
]
