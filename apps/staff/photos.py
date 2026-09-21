"""
Staff photos are stored as data URLs in the row itself, and every list that
shows a staff member sends the photo along. So they must be small: an avatar
is never shown larger than about 160 px, and a full-size phone photo is
several hundred kilobytes per row.

The frontend already shrinks a photo before upload; this is the server's own
guarantee, so a client that skips that step cannot store a large one.
"""

import base64
from io import BytesIO

from PIL import Image, ImageOps

AVATAR_PX = 160
JPEG_QUALITY = 82
#: Anything this size or smaller is already an avatar; left exactly as sent.
SHRINK_ABOVE_CHARS = 40_000


def shrink_photo(data_url: str) -> str:
    """
    A square 160 px JPEG data URL for this photo, or the photo unchanged.

    Unchanged when it is empty, already small, not a data URL, or not an
    image Pillow can read — a photo is never lost to this, only made smaller.
    """
    if not data_url or len(data_url) <= SHRINK_ABOVE_CHARS:
        return data_url
    if not data_url.startswith("data:image/") or "," not in data_url:
        return data_url

    try:
        raw = base64.b64decode(data_url.split(",", 1)[1], validate=True)
        with Image.open(BytesIO(raw)) as source:
            image = ImageOps.exif_transpose(source)
            if image.mode in ("RGBA", "LA", "P"):
                # Transparent areas become white, not the black a plain RGB
                # conversion would give them.
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
