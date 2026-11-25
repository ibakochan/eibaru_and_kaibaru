from django.db import migrations, models
from django.db.models import OuterRef, Subquery


def copy_club_values_onto_lessons(apps, schema_editor):
    """Lessons used to inherit a blank cap or fee from the club at booking time.

    Store that current club value on the lesson so existing bookings keep the
    same limit after a blank lesson field means unlimited.
    """
    Lesson = apps.get_model("kaibaru", "Lesson")
    Club = apps.get_model("kaibaru", "Club")
    pairs = (
        ("member_reservation_max_count", "member_reservation_max_count"),
        ("member_reservation_max_days_ahead", "member_reservation_max_days_ahead"),
        ("visitor_reservation_max_count", "visitor_reservation_max_count"),
        ("visitor_reservation_max_days_ahead", "visitor_reservation_max_days_ahead"),
        ("instructor_fee", "default_instructor_lesson_fee"),
    )
    for lesson_field, club_field in pairs:
        club_value = Club.objects.filter(pk=OuterRef("club_id")).values(club_field)[:1]
        (
            Lesson.objects.filter(**{f"{lesson_field}__isnull": True})
            .update(**{lesson_field: Subquery(club_value)})
        )


class Migration(migrations.Migration):

    dependencies = [
        ("kaibaru", "0177_workertimeoff_paid"),
    ]

    operations = [
        migrations.AddField(
            model_name="club",
            name="default_reservation_only",
            field=models.BooleanField(
                default=False,
                help_text="Starting value for a new lesson: join only by reservation.",
            ),
        ),
        migrations.AddField(
            model_name="club",
            name="default_reservation_limit",
            field=models.PositiveIntegerField(
                blank=True,
                help_text="Starting reservation capacity for a new lesson. Empty means unlimited.",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="club",
            name="lesson_reservation_payment_options",
            field=models.CharField(
                choices=[
                    ("stripe", "Card (Stripe)"),
                    ("cash", "Pay at the venue"),
                    ("both", "Card or pay at the venue"),
                ],
                default="stripe",
                help_text="Starting payment method for a new lesson. stripe, cash, or both.",
                max_length=10,
            ),
        ),
        migrations.AddField(
            model_name="club",
            name="default_lesson_instructors",
            field=models.ManyToManyField(
                blank=True,
                limit_choices_to={"is_instructor": True},
                related_name="default_instructor_for_clubs",
                to="kaibaru.member",
            ),
        ),
        migrations.AlterField(
            model_name="club",
            name="default_instructor_lesson_fee",
            field=models.PositiveIntegerField(
                blank=True,
                help_text="Yen copied into a new lesson as its instructor fee. Empty means no fee.",
                null=True,
            ),
        ),
        migrations.AlterField(
            model_name="club",
            name="member_reservation_max_count",
            field=models.PositiveIntegerField(
                blank=True,
                help_text="Starting member-reservation cap copied into a new lesson. Empty means unlimited.",
                null=True,
            ),
        ),
        migrations.AlterField(
            model_name="club",
            name="visitor_reservation_max_count",
            field=models.PositiveIntegerField(
                blank=True,
                help_text="Starting visitor-reservation cap copied into a new lesson. Empty means unlimited.",
                null=True,
            ),
        ),
        migrations.AlterField(
            model_name="lesson",
            name="instructor_fee",
            field=models.PositiveIntegerField(
                blank=True,
                help_text="Yen paid to each instructor for this lesson. Empty means no instructor fee.",
                null=True,
            ),
        ),
        migrations.AlterField(
            model_name="lesson",
            name="member_reservation_max_count",
            field=models.PositiveIntegerField(
                blank=True,
                help_text="Maximum upcoming member reservations for this lesson. Empty means unlimited.",
                null=True,
            ),
        ),
        migrations.AlterField(
            model_name="lesson",
            name="member_reservation_max_days_ahead",
            field=models.PositiveIntegerField(
                blank=True,
                help_text="How many days ahead a member may reserve this lesson. Empty means no date horizon. 0 means today only.",
                null=True,
            ),
        ),
        migrations.AlterField(
            model_name="lesson",
            name="visitor_reservation_max_count",
            field=models.PositiveIntegerField(
                blank=True,
                help_text="Maximum upcoming visitor reservations for this lesson per email. Empty means unlimited.",
                null=True,
            ),
        ),
        migrations.AlterField(
            model_name="lesson",
            name="visitor_reservation_max_days_ahead",
            field=models.PositiveIntegerField(
                blank=True,
                help_text="How many days ahead a visitor may reserve this lesson. Empty means no date horizon. 0 means today only.",
                null=True,
            ),
        ),
        migrations.RunPython(copy_club_values_onto_lessons, migrations.RunPython.noop),
    ]
