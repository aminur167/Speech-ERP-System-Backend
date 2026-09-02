from django.db import migrations


def backfill_branch(apps, schema_editor):
    """
    Services predate branch-scoping and have no branch of their own.

    There is no correct answer for which branch an org-wide package
    "belongs" to -- fall back to the oldest branch (typically the only one
    on a small deployment) so nothing is silently dropped. A multi-branch
    deployment carrying real branchless catalog data should reassign these
    by hand afterward; this migration only guarantees the NOT NULL
    constraint added next can be applied safely.
    """
    Service = apps.get_model("services", "Service")
    Branch = apps.get_model("branches", "Branch")

    # The historical model's default manager (no use_in_migrations custom
    # manager is declared) is unfiltered, so this also reaches soft-deleted
    # rows -- they need a valid branch too, since the next migration makes
    # the column NOT NULL for every row regardless of is_deleted.
    orphaned = Service.objects.filter(branch__isnull=True)
    if not orphaned.exists():
        return

    first_branch = Branch.objects.order_by("id").first()
    if first_branch is None:
        return
    orphaned.update(branch_id=first_branch.id)


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('services', '0003_service_branch_nullable'),
    ]

    operations = [
        migrations.RunPython(backfill_branch, noop_reverse),
    ]
