"""
Give every existing monthly package an admit fee equal to its monthly fee.

Admit fee is required on monthly packages from now on, and the client chose
this default for the packages that predate it: enrolling a new patient keeps
costing what it cost before until someone edits the package. Soft-deleted
rows are included so a restored package is never left without one.
"""

from django.db import migrations
from django.db.models import F


def backfill(apps, schema_editor):
    Service = apps.get_model("services", "Service")
    # _base_manager, never a default manager that could filter out
    # soft-deleted rows: every row gets the value, whatever its state.
    Service._base_manager.filter(category="monthly", admission_fee__isnull=True).update(
        admission_fee=F("fee")
    )


class Migration(migrations.Migration):

    dependencies = [
        ("services", "0007_service_admission_fee"),
    ]

    # Reversing leaves the values in place: the column itself is dropped by
    # reversing 0007, and blanking them here would lose nothing but time.
    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
