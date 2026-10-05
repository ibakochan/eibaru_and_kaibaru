from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import EventReservation, MemberRecord, Test, TestResult


def event_test(event):
    try:
        return event.test
    except Test.DoesNotExist:
        return None


def visible_test_results(event, user):
    test = event_test(event)
    if test is None:
        return None

    rows = EventReservation.objects.filter(
        event=event,
        is_deleted=False,
        canceled=False,
        status=EventReservation.Status.PAID,
    ).order_by("full_name", "id")

    if not test.results_public:
        if user is None or not getattr(user, "is_authenticated", False):
            rows = rows.none()
        elif event.club.owner_id != user.id:
            owned_member_ids = list(
                event.club.members.filter(owner=user).values_list("id", flat=True)
            )
            query = Q(member_id__in=owned_member_ids) | Q(user=user)
            email = (getattr(user, "email", "") or "").strip()
            if email:
                query |= Q(email__iexact=email)
            rows = rows.filter(query)

    scores = dict(
        TestResult.objects.filter(test=test, reservation__in=rows).values_list(
            "reservation_id",
            "score",
        )
    )
    return [
        {
            "id": row.id,
            "full_name": row.full_name,
            "result": scores.get(row.id, ""),
            "price_name": row.price_name,
            "member": row.member_id,
        }
        for row in rows
    ]


def _sync_member_record(reservation, score):
    if not reservation.member_id:
        return

    occurred_on = timezone.localtime(reservation.event.starts_at).date()
    record = MemberRecord.objects.filter(reservation=reservation).first()
    if record is None:
        MemberRecord.objects.create(
            club=reservation.club,
            member=reservation.member,
            event=reservation.event,
            reservation=reservation,
            kind=MemberRecord.Kind.TEST,
            name=(reservation.event.title or "")[:200] or "テスト",
            occurred_on=occurred_on,
            result=score,
        )
        return

    record.member = reservation.member
    record.event = reservation.event
    record.result = score
    record.save(update_fields=["member", "event", "result", "updated_at"])


@transaction.atomic
def set_test_result(reservation, result_text):
    test = event_test(reservation.event)
    if test is None:
        raise ValueError("テストの結果だけ入力できます。")

    text = str(result_text or "").strip()
    if len(text) > 80:
        raise ValueError("結果は80文字以内にしてください。")

    TestResult.objects.update_or_create(
        reservation=reservation,
        defaults={"test": test, "score": text},
    )
    _sync_member_record(reservation, text)
    return text


def sync_test_result_from_record(record):
    if not record.reservation_id:
        return
    reservation = record.reservation
    test = event_test(reservation.event)
    if test is None:
        return
    TestResult.objects.update_or_create(
        reservation=reservation,
        defaults={"test": test, "score": record.result},
    )
