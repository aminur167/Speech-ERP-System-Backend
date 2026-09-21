"""
Shrink staff photos saved before photos were resized on upload.

Those are full-size images, sent with every list that shows staff. This makes
each one a 160 px square JPEG — the largest size it is ever displayed at.
Photos that are already small, or that cannot be read as images, are left
exactly as they are.

Self-contained on purpose: a migration must keep working even if the app's
own helper (apps/staff/photos.py) changes later.

Not reversible — the original resolution is not kept. Reversing is a no-op.
"""

import base64
from io import BytesIO

from django.db import migrations

AVATAR_PX = 160
JPEG_QUALITY = 82
SHRINK_ABOVE_CHARS = 40_000


def _shrink(data_url):
    from PIL import Image, ImageOps

    if not data_url or len(data_url) <= SHRINK_ABOVE_CHARS:
        return data_url
    if not data_url.startswith("data:image/") or "," not in data_url:
        return data_url
    try:
        raw = base64.b64decode(data_url.split(",", 1)[1], validate=True)
        with Image.open(BytesIO(raw)) as source:
            image = ImageOps.exif_transpose(source)
            if image.mode in ("RGBA", "LA", "P"):
                image = image.convert("RGBA")
                background = Image.new("RGB", image.size, "white")
                background.paste(image, mask=image.getchannel("A"))
                image = background
            else:
                image = image.convert("RGB")
            image = ImageOps.fit(image, (AVATAR_PX, AVATAR_PX), Image.Resampling.LANCZOS)
            out = BytesIO()
            image.save(out, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    except (ValueError, OSError, Image.DecompressionBombError):
        return data_url
    shrunk = "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode("ascii")
    return shrunk if len(shrunk) < len(data_url) else data_url


def shrink_existing(apps, schema_editor):
    StaffMember = apps.get_model("staff", "StaffMember")
    rows = StaffMember._base_manager.exclude(photo_url="").only("id", "photo_url")
    for member in rows.iterator(chunk_size=50):
        smaller = _shrink(member.photo_url)
        if smaller != member.photo_url:
            StaffMember._base_manager.filter(pk=member.pk).update(photo_url=smaller)


class Migration(migrations.Migration):
    dependencies = [
        ("staff", "0005_staffmember_photo_url"),
    ]

    operations = [
        migrations.RunPython(shrink_existing, migrations.RunPython.noop),
    ]
