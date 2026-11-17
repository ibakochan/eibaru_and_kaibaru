from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("kaibaru", "0171_remove_teacherinvoicelesson_unique_invoice_lesson_occurrence_and_more"),
    ]

    operations = [
        migrations.AlterField(
            model_name="club",
            name="visitor_booking_confirm",
            field=models.CharField(
                choices=[("email_link", "Email link"), ("instant", "Instant")],
                default="instant",
                help_text=(
                    "How visitors finish salon, lesson, trial, and event bookings: "
                    "email start link, or confirm/checkout immediately in the form."
                ),
                max_length=20,
            ),
        ),
    ]
