from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class ProfileSettingsMigrationTests(TransactionTestCase):
    migrate_from = [('w_craft_back', '0062_project_progress_sources')]
    migrate_to = [
        ('w_craft_back', '0063_user_notification_preferences_and_comments'),
    ]

    def setUp(self):
        super().setUp()
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        old_apps = executor.loader.project_state(self.migrate_from).apps
        User = old_apps.get_model('auth', 'User')
        UserProfile = old_apps.get_model('w_craft_back', 'UserProfile')
        english_user = User.objects.create(username='english-settings-user')
        russian_user = User.objects.create(username='russian-settings-user')
        self.english_profile_id = UserProfile.objects.create(
            user=english_user,
            language='en',
            notifications_enabled=False,
        ).pk
        self.russian_profile_id = UserProfile.objects.create(
            user=russian_user,
            language='ru',
            notifications_enabled=True,
        ).pk

        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)
        self.apps = executor.loader.project_state(self.migrate_to).apps

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def test_preserves_notification_preference_and_initializes_content_language(self):
        UserProfile = self.apps.get_model('w_craft_back', 'UserProfile')
        english = UserProfile.objects.get(pk=self.english_profile_id)
        russian = UserProfile.objects.get(pk=self.russian_profile_id)
        self.assertEqual(english.content_language, 'en')
        self.assertFalse(english.notifications_in_app)
        self.assertEqual(russian.content_language, 'ru')
        self.assertTrue(russian.notifications_in_app)
