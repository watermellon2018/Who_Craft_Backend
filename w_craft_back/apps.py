from django.apps import AppConfig


class WCraftBackConfig(AppConfig):
    default_auto_field = 'django.db.models.AutoField'
    name = 'w_craft_back'

    def ready(self):
        from w_craft_back import storage_signals  # noqa: F401
        from w_craft_back.movie.storyboard import signals  # noqa: F401
