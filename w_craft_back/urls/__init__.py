from w_craft_back.urls.urls import urlpatterns as login_urls
from w_craft_back.urls.character_display import urlpatterns as character_display

urlpatterns = login_urls + character_display
