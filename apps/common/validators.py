"""
Shared validators.

Defined once and reused. Patient alone has three phone fields; duplicating the
regex per field is how they drift apart and start accepting different things.
"""

import re

from django.core.exceptions import ValidationError

# Bangladeshi mobile numbers: 11 digits starting 01, operator prefix 3-9.
# Accepts the forms staff actually type — 01711000001, +8801711000001,
# 8801711000001, and any of those with spaces or dashes.
_BD_MOBILE = re.compile(r"^(?:\+?880|0)1[3-9]\d{8}$")


def normalize_phone(value: str) -> str:
    """
    Reduce a number to one canonical stored form: `01XXXXXXXXX`.

    Without this, the same person entered as `+880 1711-000001` and
    `01711000001` produces two rows that don't match each other in search —
    a real duplicate-patient source in clinic systems.
    """
    if not value:
        return value

    digits = re.sub(r"[\s\-()]", "", value.strip())

    if digits.startswith("+880"):
        digits = "0" + digits[4:]
    elif digits.startswith("880"):
        digits = "0" + digits[3:]

    return digits


def validate_bd_phone(value: str) -> None:
    """Validator for BD mobile numbers. Attach to model and serializer fields."""
    if not value:
        return

    normalized = normalize_phone(value)
    if not _BD_MOBILE.match(normalized):
        raise ValidationError(
            "Enter a valid Bangladeshi mobile number, e.g. 01711000001.",
            code="invalid_phone",
        )
