from django.test import TestCase, Client
from django.urls import reverse
from rest_framework import status


class MyAPIEndpointTestCase(TestCase):

    def test_api_request(self):
        client = Client()

        endpoint_url = reverse('generate_image')

        response = client.get(endpoint_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
