from django.urls import path

from w_craft_back.characters.pages.graph.view import select_all_type_relationship, \
    select_edges, update_info_edge, delete_edge, add_edges


urlpatterns = [
    path('relation/', select_all_type_relationship, name='select_all_type_relationship'),
    path('select/', select_edges, name='select_edges'),
    path('add/', add_edges, name='add_edges'),
    path('delete/', delete_edge, name='delete_edge'),
    path('update/', update_info_edge, name='update_info_edge'),
]
