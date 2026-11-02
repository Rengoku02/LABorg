from django.urls import path

from . import search_views, views

app_name = "bench"

urlpatterns = [
    path("i/<str:short_id>", views.item_page, name="item"),
    path("i/<str:short_id>/share", views.item_share, name="item_share"),
    path("l/<str:code>", views.location_page, name="location"),
    path("l/<str:code>/photo", views.location_photo, name="location_photo"),
    path("me/stock", views.my_stock, name="my_stock"),
    path("claims/", views.claims_list, name="claims"),
    path("claims/t/<str:token>/", views.claims_token, name="claims_token"),
    path("claims/<int:pk>/", views.claim_decide, name="claim_decide"),
    path("claims/champions/", views.champions, name="champions"),
    path("add/", views.add, name="add"),
    path("add/products", views.add_products, name="add_products"),
    path("boxes/new", views.box_new, name="box_new"),
    path("s", search_views.search_page, name="search"),
    path("s/setup/", search_views.search_setup, name="search_setup"),
    path("opensearch.xml", search_views.opensearch, name="opensearch"),
    path("asks/", search_views.asks_page, name="asks"),
    path("asks/owner/<str:short_id>", search_views.ask_owner_view, name="ask_owner"),
    path("asks/lab/", search_views.ask_lab_view, name="ask_lab"),
    path("board/", search_views.board, name="board"),
]
