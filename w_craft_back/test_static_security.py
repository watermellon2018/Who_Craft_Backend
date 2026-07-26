from pathlib import Path
from types import ModuleType

from django.conf import settings
from django.test import Client, SimpleTestCase, override_settings
from django.urls import clear_url_caches

from backend.urls import development_static_urlpatterns


class StaticFilesSecurityTests(SimpleTestCase):
    def test_static_root_is_separate_from_project_root(self):
        project_root = Path(settings.BASE_DIR).resolve()
        static_root = Path(settings.STATIC_ROOT).resolve()
        media_root = Path(settings.MEDIA_ROOT).resolve()

        self.assertNotEqual(static_root, project_root)
        self.assertNotIn(static_root, media_root.parents)
        self.assertNotIn(project_root, media_root.parents)

    @override_settings(DEBUG=False)
    def test_static_and_media_routes_are_disabled_without_debug(self):
        self.assertEqual(development_static_urlpatterns(), [])

    @override_settings(DEBUG=True)
    def test_source_and_secret_paths_are_not_served_from_static(self):
        urlconf = ModuleType("static_security_test_urls")
        urlconf.urlpatterns = development_static_urlpatterns()

        with override_settings(ROOT_URLCONF=urlconf):
            clear_url_caches()
            try:
                client = Client()
                for path in (
                    "/static/.env",
                    "/static/manage.py",
                    "/static/postgress_db.json",
                ):
                    with self.subTest(path=path):
                        self.assertEqual(client.get(path).status_code, 404)
            finally:
                clear_url_caches()
