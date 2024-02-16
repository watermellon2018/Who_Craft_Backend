from django.urls import path

from w_craft_back.views.character_views import CharacterTree

urlpatterns = [
    path('characters/', CharacterTree.as_view(), name='characters'),
]
