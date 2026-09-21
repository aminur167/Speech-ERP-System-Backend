"""API responses are gzip-compressed when the client accepts it — except auth."""

import pytest

pytestmark = pytest.mark.django_db


def test_a_list_is_compressed(manager_client, patient_factory):
    for _ in range(10):
        patient_factory()

    response = manager_client.get("/api/patients/", HTTP_ACCEPT_ENCODING="gzip")

    assert response.status_code == 200
    assert response["Content-Encoding"] == "gzip"


def test_not_compressed_when_the_client_does_not_ask(manager_client, patient_factory):
    for _ in range(10):
        patient_factory()

    response = manager_client.get("/api/patients/")

    assert not response.has_header("Content-Encoding")


def test_auth_responses_are_never_compressed(api_client, manager):
    response = api_client.post(
        "/api/auth/login/",
        {"email": manager.email, "password": "wrong-password"},
        format="json",
        HTTP_ACCEPT_ENCODING="gzip",
    )

    assert not response.has_header("Content-Encoding")
