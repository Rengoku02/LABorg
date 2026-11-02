from django.urls import path

from . import views

app_name = "quartzy"

urlpatterns = [
    path("health/", views.health, name="health"),
]
