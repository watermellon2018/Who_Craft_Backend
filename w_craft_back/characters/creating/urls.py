from w_craft_back.characters.creating.view import create_hero
from django.urls import path


urlpatterns = [
    # path('get/', CharacterTree.as_view(), name='get_hero'),
    # path('delete/', CharacterTree.as_view(), name='delete_hero'),
    path('create/', create_hero, name='create_hero'),
    # path('rename/', rename_character, name='rename_hero'),
]
