from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('services', '0004_backfill_service_branch'),
    ]

    operations = [
        migrations.AlterField(
            model_name='service',
            name='branch',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name='services',
                to='branches.branch',
            ),
        ),
        migrations.AddIndex(
            model_name='service',
            index=models.Index(fields=['branch', 'category', 'is_active'], name='services_se_branch__0a7b94_idx'),
        ),
        migrations.AddConstraint(
            model_name='service',
            constraint=models.UniqueConstraint(fields=('branch', 'code'), name='unique_service_code_per_branch'),
        ),
    ]
