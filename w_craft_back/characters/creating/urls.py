from w_craft_back.characters.creating.view import create_hero, select_all
from django.urls import path


urlpatterns = [
    # path('get/', CharacterTree.as_view(), name='get_hero'),
    # path('delete/', CharacterTree.as_view(), name='delete_hero'),
    path('create/', create_hero, name='create_hero'),
    path('select/', select_all, name='select_all'),
    # path('rename/', rename_character, name='rename_hero'),
]
