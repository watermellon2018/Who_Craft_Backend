from uuid import uuid4

from django.test import SimpleTestCase

from w_craft_back.messaging.serializers import (
    EditMessageSerializer,
    SendMessageSerializer,
)


class MessageTextContractTests(SimpleTestCase):
    def test_send_and_edit_reject_nonstring_json_text(self) -> None:
        for value in (123, 1.5, True, [], {}):
            for serializer_type in (SendMessageSerializer, EditMessageSerializer):
                with self.subTest(value=value, serializer=serializer_type):
                    serializer = serializer_type(data={
                        'text': value,
                        'client_id': str(uuid4()),
                        'expected_version': 1,
                    })
                    self.assertFalse(serializer.is_valid())
                    self.assertIn('text', serializer.errors)
