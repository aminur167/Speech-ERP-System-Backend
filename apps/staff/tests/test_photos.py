"""Staff photos are stored as small avatars, whatever the client sends."""

import base64
import importlib
from io import BytesIO

import pytest
from PIL import Image

from apps.staff.models import StaffMember
from apps.staff.photos import AVATAR_PX, shrink_photo

pytestmark = pytest.mark.django_db


def data_url(size=(1200, 900), fmt="PNG", mode="RGB", color=(200, 30, 30)):
    image = Image.new(mode, size, color)
    # Noise so the encoder can't compress it to nothing.
    pixels = image.load()
    for x in range(0, size[0], 3):
        for y in range(0, size[1], 3):
            pixels[x, y] = (x % 256, y % 256, (x * y) % 256) + ((255,) if mode == "RGBA" else ())
    out = BytesIO()
    image.save(out, format=fmt)
    mime = "png" if fmt == "PNG" else "jpeg"
    return f"data:image/{mime};base64," + base64.b64encode(out.getvalue()).decode()


def decode(url):
    return Image.open(BytesIO(base64.b64decode(url.split(",", 1)[1])))


def test_a_large_photo_becomes_a_small_square_jpeg():
    big = data_url()

    small = shrink_photo(big)

    assert small.startswith("data:image/jpeg;base64,")
    assert len(small) < len(big) / 10
    assert decode(small).size == (AVATAR_PX, AVATAR_PX)


def test_transparency_turns_white_not_black():
    # Transparent background with opaque noise on every third pixel (which
    # also makes it large enough to be shrunk).
    url = data_url(size=(1000, 1000), mode="RGBA", color=(0, 0, 0, 0))

    small = shrink_photo(url)

    corner = decode(small).convert("RGB").getpixel((1, 1))
    assert min(corner) > 150, corner


def test_small_photos_are_left_exactly_as_sent():
    small = data_url(size=(40, 40))
    assert shrink_photo(small) == small


@pytest.mark.parametrize(
    "value",
    ["", "https://example.com/photo.jpg", "data:image/png;base64," + "!" * 50_000],
    # Short ids: pytest puts the id in an environment variable, which Windows
    # caps at 32,767 characters.
    ids=["empty", "plain-url", "not-base64"],
)
def test_anything_unreadable_is_left_alone(value):
    assert shrink_photo(value) == value


def test_the_api_stores_the_shrunk_photo(manager_client, branch):
    big = data_url()
    response = manager_client.post(
        "/api/staff/",
        {
            "name": "Photo Test",
            "designation": "therapist",
            "phone": "01712345678",
            "joined_at": "2026-01-01",
            "monthly_salary": "20000.00",
            "photo_url": big,
        },
        format="json",
    )

    assert response.status_code == 201, response.json()
    stored = StaffMember.objects.get(pk=response.json()["id"]).photo_url
    assert decode(stored).size == (AVATAR_PX, AVATAR_PX)


def test_the_migration_shrinks_existing_photos(staff_member_factory):
    big = data_url()
    member = staff_member_factory(photo_url=big)
    untouched = staff_member_factory(photo_url="")

    migration = importlib.import_module("apps.staff.migrations.0006_shrink_existing_staff_photos")
    from django.apps import apps

    migration.shrink_existing(apps, None)

    member.refresh_from_db()
    untouched.refresh_from_db()
    assert decode(member.photo_url).size == (AVATAR_PX, AVATAR_PX)
    assert untouched.photo_url == ""
