from django.urls import path

from . import views

app_name = "guard"

urlpatterns = [
    path("label/", views.label_list, name="label_list"),
    path("label/<int:pk>/", views.label_match, name="label_match"),
]
