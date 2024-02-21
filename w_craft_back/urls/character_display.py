from django.urls import path

from w_craft_back.views.character_views import CharacterTree,\
    create_character, rename_character

urlpatterns = [
    path('characters/', CharacterTree.as_view(), name='characters'),
    path('delete/', CharacterTree.as_view(), name='delete_character'),
    path('create/', create_character, name='create_character'),
    path('rename/', rename_character, name='rename_character'),
]
