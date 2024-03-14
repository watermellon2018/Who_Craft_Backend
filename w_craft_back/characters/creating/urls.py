from w_craft_back.characters.creating.view import create_hero, select_all,\
    select_hero_by_id, delete_hero_by_id, update_personal_info
from django.urls import path


urlpatterns = [
    path('select_by_id/', select_hero_by_id, name='select_hero_by_id'),
    path('delete_by_id/', delete_hero_by_id, name='delete_hero_by_id'),
    path('create/', create_hero, name='create_hero'),
    path('select/', select_all, name='select_all'),
    path('update_personal_info/', update_personal_info, name='update_personal_info'),
]
