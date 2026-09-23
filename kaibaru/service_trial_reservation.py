import json
import secrets

from django.db import IntegrityError, transaction

from .models import Club, Lesson, Reservation
from .rules_eligibility import assert_visitor_eligible_for_lesson
from .rules_reservations import (
    assert_reservation_horizon,
    display_reservation_name,
    hold_cutoff_at,
    hold_is_active,
    make_reservation_key,
    normalize_reservation_name,
    request_is_fresh,
)
from .service_reservation_start import queue_reservation_start_email


MAX_TRIAL_PARTICIPANTS = 10


class TrialReservationService:

    @staticmethod
    def participants_from_post(post):
        raw = post.get("participants", "").strip()

        if raw:
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    "予約者情報の形式が正しくありません。"
                ) from exc

            if not isinstance(payload, list):
                raise ValueError(
                    "予約者情報の形式が正しくありません。"
                )

            return payload

        return [
            {
                "full_name": post.get("full_name", ""),
                "age": post.get("age", ""),
                "gender": post.get("gender", ""),
            }
        ]

    @staticmethod
    def create_reservations(
        *,
        club,
        lesson,
        reservation_date,
        email,
        phone_number="",
        user=None,
        participants,
    ):
        prepared = _prepare_trial_request(
            club=club,
            lesson=lesson,
            reservation_date=reservation_date,
            email=email,
            phone_number=phone_number,
            user=user,
            participants=participants,
        )

        try:
            with transaction.atomic():
                token, reservation_ids = _save_trial_reservations(
                    **prepared
                )
                queue_reservation_start_email(token)
        except IntegrityError:
            existing = (
                Reservation.objects
                .filter(reservation_key__in=prepared["keys"])
                .exclude(start_token="")
                .first()
            )

            if existing and existing.start_token:
                from .tasks_emails import (
                    send_reservation_start_email,
                )

                send_reservation_start_email.delay(
                    existing.start_token
                )

                return _email_sent_result([existing.id])

            raise ValueError(
                "この体験予約はすでに申し込み済みです。"
                "メールをご確認ください。"
            )

        return _email_sent_result(reservation_ids)


def _email_sent_result(reservation_ids):
    return {
        "success": True,
        "email_sent": True,
        "requires_checkout": False,
        "paid": False,
        "reservation_id": reservation_ids[0],
        "reservation_ids": reservation_ids,
    }


def _prepare_trial_request(
    *,
    club,
    lesson,
    reservation_date,
    email,
    phone_number,
    user,
    participants,
):
    if lesson.club_id != club.id:
        raise ValueError(
            "このレッスンは指定されたクラブに属していません。"
        )

    if user is not None:
        from .models import Member

        if Member.objects.filter(club=club, user=user).exists():
            raise ValueError(
                "会員の方は体験予約をご利用できません。"
            )

    if club.trials_disabled:
        raise ValueError(
            "現在、体験予約を受け付けていません。"
        )

    if lesson.trial_disabled:
        raise ValueError(
            "このレッスンでは体験予約を受け付けていません。"
        )

    if lesson.trial_price is not None:
        reservation_price = lesson.trial_price
    else:
        reservation_price = club.trial_price

    if reservation_price is None:
        reservation_price = 0

    if lesson.weekday != reservation_date.weekday():
        raise ValueError(
            "選択した日付がレッスンの曜日と一致していません。"
        )

    assert_reservation_horizon(
        reservation_date=reservation_date,
        max_days_ahead=None,
    )

    if reservation_price > 0 and not club.stripe_account_id:
        raise ValueError(
            "このクラブではオンライン決済が設定されていません。"
        )

    if user is not None:
        email = (user.email or "").strip().lower()

        if not email:
            raise ValueError(
                "アカウントにメールアドレスが"
                "登録されていません。"
            )
    else:
        email = (email or "").strip().lower()

        if not email:
            raise ValueError(
                "メールアドレスを入力してください。"
            )

    people = _clean_participants(
        participants,
        lesson=lesson,
    )

    phone_number = (phone_number or "").strip()

    keys = [
        make_reservation_key(
            kind="trial",
            lesson_id=lesson.id,
            reservation_date=reservation_date,
            email=email,
            normalized_name=person["normalized_name"],
        )
        for person in people
    ]

    return {
        "club": club,
        "lesson": lesson,
        "reservation_date": reservation_date,
        "email": email,
        "phone_number": phone_number,
        "user": user,
        "people": people,
        "reservation_price": reservation_price,
        "keys": keys,
    }


def _clean_participants(participants, *, lesson):
    if not isinstance(participants, list) or not participants:
        raise ValueError("予約者を入力してください。")

    if len(participants) > MAX_TRIAL_PARTICIPANTS:
        raise ValueError(
            f"体験予約は一度に{MAX_TRIAL_PARTICIPANTS}人までです。"
        )

    cleaned = []
    seen = set()

    for index, raw in enumerate(participants, start=1):
        if not isinstance(raw, dict):
            raise ValueError(
                "予約者情報の形式が正しくありません。"
            )

        full_name = display_reservation_name(
            raw.get("full_name", "")
        )
        label = full_name or f"{index}人目"

        if not full_name:
            raise ValueError(
                f"{label}のお名前を入力してください。"
            )

        normalized_name = normalize_reservation_name(full_name)

        if normalized_name in seen:
            raise ValueError(
                "同じお名前が複数入力されています。"
            )

        seen.add(normalized_name)

        age = _clean_age(raw.get("age", None), label)
        gender = _clean_gender(raw.get("gender", ""), label)

        try:
            assert_visitor_eligible_for_lesson(
                age=age,
                gender=gender,
                lesson=lesson,
            )
        except ValueError as exc:
            if len(participants) > 1:
                raise ValueError(f"{label}さん: {exc}") from exc
            raise

        cleaned.append(
            {
                "full_name": full_name,
                "normalized_name": normalized_name,
                "age": age,
                "gender": gender,
            }
        )

    return cleaned


def _clean_age(raw_age, label):
    if raw_age is None or raw_age == "":
        return None

    if isinstance(raw_age, bool):
        raise ValueError(
            f"{label}の年齢の形式が正しくありません。"
        )

    if isinstance(raw_age, float):
        if not raw_age.is_integer():
            raise ValueError(
                f"{label}の年齢の形式が正しくありません。"
            )
        raw_age = int(raw_age)

    try:
        age = int(raw_age)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{label}の年齢の形式が正しくありません。"
        ) from exc

    if age < 0 or age > 120:
        raise ValueError(
            f"{label}の年齢の形式が正しくありません。"
        )

    return age


def _clean_gender(raw_gender, label):
    gender = str(raw_gender or "").strip()

    if gender and gender not in ("male", "female"):
        raise ValueError(
            f"{label}の性別の指定が正しくありません。"
        )

    return gender


def _save_trial_reservations(
    *,
    club,
    lesson,
    reservation_date,
    email,
    phone_number,
    user,
    people,
    reservation_price,
    keys,
):
    from django.utils import timezone

    now = timezone.now()
    hold_cutoff = hold_cutoff_at(now)

    Club.objects.select_for_update().get(pk=club.pk)
    locked_lesson = (
        Lesson.objects
        .select_for_update()
        .get(id=lesson.id)
    )

    existing_rows = list(
        Reservation.objects
        .select_for_update()
        .filter(
            club=club,
            reservation_type=Reservation.ReservationType.TRIAL,
            email__iexact=email,
        )
    )

    grouped = []

    for person in people:
        rows = [
            row
            for row in existing_rows
            if normalize_reservation_name(row.full_name)
            == person["normalized_name"]
        ]
        row, kind = _classify_trial_rows(
            rows,
            now=now,
            hold_cutoff=hold_cutoff,
        )
        grouped.append((person, rows, row, kind))

    resend_rows = _matching_pending_group(
        grouped,
        lesson=locked_lesson,
        reservation_date=reservation_date,
    )

    if resend_rows is not None:
        for person in people:
            row = next(
                item
                for item in resend_rows
                if normalize_reservation_name(item.full_name)
                == person["normalized_name"]
            )
            row.full_name = person["full_name"]
            row.phone_number = phone_number
            row.age = person["age"]
            row.gender = person["gender"]
            row.amount = reservation_price
            row.user = user
            row.requested_at = now
            row.save(
                update_fields=[
                    "full_name",
                    "phone_number",
                    "age",
                    "gender",
                    "amount",
                    "user",
                    "requested_at",
                ]
            )

        return (
            resend_rows[0].start_token,
            [row.id for row in resend_rows],
        )

    for person, rows, row, kind in grouped:
        if kind in ("paid", "unpaid"):
            raise ValueError(
                _trial_blocker_message(
                    person["full_name"],
                    kind,
                )
            )

    deleted_ids = set()
    tokens_to_clear = set()

    for person, rows, row, kind in grouped:
        if kind == "pending" and row.start_token:
            tokens_to_clear.add(row.start_token)

        for existing in _replaceable_trial_rows(
            rows,
            hold_cutoff=hold_cutoff,
        ):
            if existing.id in deleted_ids:
                continue

            deleted_ids.add(existing.id)
            existing.delete()

    if tokens_to_clear:
        leftover = (
            Reservation.objects
            .select_for_update()
            .filter(
                start_token__in=tokens_to_clear,
                status=Reservation.Status.NOT_STARTED,
            )
        )

        for existing in leftover:
            if existing.id in deleted_ids:
                continue

            deleted_ids.add(existing.id)
            existing.delete()

    token = secrets.token_urlsafe(32)
    created_ids = []

    for person, key in zip(people, keys):
        reservation = Reservation.objects.create(
            lesson=locked_lesson,
            club=club,
            member=None,
            user=user,
            payment_method="stripe",
            reservation_type=Reservation.ReservationType.TRIAL,
            status=Reservation.Status.NOT_STARTED,
            full_name=person["full_name"],
            email=email,
            phone_number=phone_number,
            age=person["age"],
            gender=person["gender"],
            amount=reservation_price,
            currency="jpy",
            reservation_date=reservation_date,
            reservation_key=key,
            start_token=token,
            requested_at=now,
        )
        created_ids.append(reservation.id)

    return token, created_ids


def _classify_trial_rows(rows, *, now, hold_cutoff):
    for row in rows:
        if row.status == Reservation.Status.PAID:
            return row, "paid"

    for row in rows:
        if (
            row.status == Reservation.Status.UNPAID
            and hold_is_active(row, hold_cutoff)
        ):
            return row, "unpaid"

    for row in rows:
        if (
            row.status == Reservation.Status.NOT_STARTED
            and request_is_fresh(row, now)
            and row.start_token
        ):
            return row, "pending"

    return None, None


def _matching_pending_group(grouped, *, lesson, reservation_date):
    if any(item[3] != "pending" for item in grouped):
        return None

    rows = [row for _, _, row, _ in grouped]
    tokens = {row.start_token for row in rows}

    if len(tokens) != 1:
        return None

    token_count = (
        Reservation.objects
        .filter(
            start_token=rows[0].start_token,
            status=Reservation.Status.NOT_STARTED,
        )
        .count()
    )

    if token_count != len(rows):
        return None

    if any(
        row.lesson_id != lesson.id
        or row.reservation_date != reservation_date
        for row in rows
    ):
        return None

    return rows


def _replaceable_trial_rows(rows, *, hold_cutoff):
    replaceable = []

    for row in rows:
        if row.status == Reservation.Status.PAID:
            continue

        if (
            row.status == Reservation.Status.UNPAID
            and hold_is_active(row, hold_cutoff)
        ):
            continue

        replaceable.append(row)

    return replaceable


def _trial_blocker_message(name, kind):
    if kind == "paid":
        return (
            f"{name}さんは、このクラブで体験予約を"
            "すでにご利用済みです。"
        )

    return (
        f"{name}さんの体験予約はお支払い手続き中です。"
        "メールのリンクからお支払いを続けてください。"
    )
