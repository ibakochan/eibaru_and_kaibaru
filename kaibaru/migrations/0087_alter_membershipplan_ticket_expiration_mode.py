# Generated manually to rename the "end_of_month" ticket expiration
# mode to "one_month" (expires exactly one calendar month after the
# grant date, instead of at the end of the grant's calendar month).

from django.db import migrations, models


def forwards_rename_end_of_month(apps, schema_editor):
    MembershipPlan = apps.get_model("kaibaru", "MembershipPlan")
    MembershipPlan.objects.filter(
        ticket_expiration_mode="end_of_month"
    ).update(ticket_expiration_mode="one_month")


def backwards_rename_one_month(apps, schema_editor):
    MembershipPlan = apps.get_model("kaibaru", "MembershipPlan")
    MembershipPlan.objects.filter(
        ticket_expiration_mode="one_month"
    ).update(ticket_expiration_mode="end_of_month")


class Migration(migrations.Migration):

    dependencies = [
        ('kaibaru', '0086_membershipplan_plan_type_and_more'),
    ]

    operations = [
        migrations.RunPython(
            forwards_rename_end_of_month,
            backwards_rename_one_month,
        ),
        migrations.AlterField(
            model_name='membershipplan',
            name='ticket_expiration_mode',
            field=models.CharField(choices=[('one_month', 'One month'), ('never', 'Never'), ('days_after_grant', 'Days after grant')], default='one_month', max_length=30),
        ),
    ]
