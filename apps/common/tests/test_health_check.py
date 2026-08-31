"""Health check — used by the frontend's real connectivity detection (docs/00)."""

import pytest
from django.urls import reverse

pytestmark = pytest.mark.django_db


def test_health_check_returns_ok_without_auth(api_client):
    response = api_client.get(reverse("health"))
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_check_does_not_touch_the_database(api_client, django_assert_max_num_queries):
    with django_assert_max_num_queries(0):
        api_client.get(reverse("health"))
