from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('branches', '0001_initial'),
        ('services', '0002_service_proposed_by_service_review_note_and_more'),
    ]

    operations = [
        migrations.RemoveIndex(
            model_name='service',
            name='services_se_categor_d8f4e5_idx',
        ),
        migrations.AddField(
            model_name='service',
            name='branch',
            field=models.ForeignKey(
                null=True,
                blank=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='services',
                to='branches.branch',
            ),
        ),
        migrations.AlterField(
            model_name='service',
            name='code',
            field=models.CharField(db_index=True, max_length=32, unique=False),
        ),
    ]
