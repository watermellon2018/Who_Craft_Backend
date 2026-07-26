"""backend URL Configuration

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/4.1/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import path, include

from django.conf import settings
from django.conf.urls.static import static
from django.urls.resolvers import URLPattern

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/character/', include('w_craft_back.characters.display_tree.urls')),
    path('api/auth/', include('w_craft_back.auth.urls')),
    path('api/projects/properties/genre/', include('w_craft_back.movie.properties.urls')),
    path('api/projects/', include('w_craft_back.movie.project.urls')),
    path('api/invitations/', include('w_craft_back.movie.project.team_urls')),
    path('api/', include('w_craft_back.character_studio.urls')),
    path('api/profile/', include('w_craft_back.profile.urls')),
    path('api/', include('w_craft_back.subscriptions.urls')),

]


def development_static_urlpatterns() -> list[URLPattern]:
    """Return local static and media routes only when Django debug mode is on."""
    if not settings.DEBUG:
        return []
    return [
        *static(settings.STATIC_URL, document_root=settings.STATIC_ROOT),
        *static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT),
    ]


urlpatterns += development_static_urlpatterns()
