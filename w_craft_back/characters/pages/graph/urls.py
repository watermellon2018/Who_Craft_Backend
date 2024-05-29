from django.urls import path

from w_craft_back.characters.pages.graph.view import select_all_type_relationship

urlpatterns = [
    path('relation/', select_all_type_relationship, name='select_all_type_relationship'),
]
