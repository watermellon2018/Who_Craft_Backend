"""Path-specific storage gateway regressions, including Windows drive escapes."""

from django.test import SimpleTestCase

from w_craft_back.storage_gateway import StorageGatewayError, safe_storage_key


class StoragePathSafetyTests(SimpleTestCase):
    def test_rejects_absolute_traversal_and_windows_drive_keys(self):
        for value in (
            "/etc/passwd",
            "../secret",
            "assets/../../secret",
            r"C:\Windows\system.ini",
            "C:/Windows/system.ini",
        ):
            with self.subTest(value=value):
                with self.assertRaises(StorageGatewayError):
                    safe_storage_key(value)

    def test_accepts_generated_relative_key(self):
        self.assertEqual(
            safe_storage_key("projects/42/assets/abc123.png"),
            "projects/42/assets/abc123.png",
        )
