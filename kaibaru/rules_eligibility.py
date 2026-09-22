from django.core.exceptions import ValidationError

from .discounts import calculate_age


GENDER_LABELS = {
    "male": "男性",
    "female": "女性",
}


def describe_age_gender_restriction(*, age_min, age_max, allowed_gender):
    gender_label = GENDER_LABELS.get(allowed_gender)
    age_part = None

    if age_min is not None and age_max is not None:
        age_part = f"{age_min}〜{age_max}歳"
    elif age_min is not None:
        age_part = f"{age_min}歳以上"
    elif age_max is not None:
        age_part = f"{age_max}歳以下"

    return gender_label, age_part


def format_restriction_message(
    *,
    noun,
    action,
    age_min,
    age_max,
    allowed_gender,
):
    gender_label, age_part = describe_age_gender_restriction(
        age_min=age_min,
        age_max=age_max,
        allowed_gender=allowed_gender,
    )

    if not gender_label and not age_part:
        return None

    if gender_label and age_part:
        return (
            f"この{noun}は{gender_label}（{age_part}）のみ{action}できます。"
        )

    if gender_label:
        return f"この{noun}は{gender_label}のみ{action}できます。"

    return f"この{noun}は{age_part}の方のみ{action}できます。"


def age_gender_block_reason(
    *,
    age,
    gender,
    age_min,
    age_max,
    allowed_gender,
    noun,
    action,
    require_missing_fields=False,
):
    """
    Return a Japanese error message if the person is not eligible,
    or None if they are eligible / there are no restrictions.
    """

    restriction_message = format_restriction_message(
        noun=noun,
        action=action,
        age_min=age_min,
        age_max=age_max,
        allowed_gender=allowed_gender,
    )

    if restriction_message is None:
        return None

    has_age_restriction = age_min is not None or age_max is not None
    has_gender_restriction = bool(allowed_gender)

    if require_missing_fields:
        if has_age_restriction and age is None:
            return f"{restriction_message}年齢を入力してください。"

        if has_gender_restriction and not gender:
            return f"{restriction_message}性別を入力してください。"

    if has_gender_restriction:
        if not gender or gender != allowed_gender:
            return restriction_message

    if has_age_restriction:
        if age is None:
            return restriction_message

        if age_min is not None and age < age_min:
            return restriction_message

        if age_max is not None and age > age_max:
            return restriction_message

    return None


def assert_age_gender_eligible(
    *,
    age,
    gender,
    age_min,
    age_max,
    allowed_gender,
    noun,
    action,
    exception_class=ValidationError,
    require_missing_fields=False,
):
    reason = age_gender_block_reason(
        age=age,
        gender=gender,
        age_min=age_min,
        age_max=age_max,
        allowed_gender=allowed_gender,
        noun=noun,
        action=action,
        require_missing_fields=require_missing_fields,
    )

    if reason:
        raise exception_class(reason)


def assert_member_eligible_for_plan(member, plan):
    if member is None or plan is None:
        return

    assert_age_gender_eligible(
        age=calculate_age(getattr(member, "birth_date", None)),
        gender=getattr(member, "gender", None),
        age_min=plan.age_min,
        age_max=plan.age_max,
        allowed_gender=plan.allowed_gender,
        noun="プラン",
        action="加入",
    )


def assert_member_eligible_for_lesson(member, lesson):
    if member is None or lesson is None:
        return

    assert_age_gender_eligible(
        age=calculate_age(getattr(member, "birth_date", None)),
        gender=getattr(member, "gender", None),
        age_min=lesson.age_min,
        age_max=lesson.age_max,
        allowed_gender=lesson.allowed_gender,
        noun="レッスン",
        action="予約",
        exception_class=ValueError,
    )


def assert_visitor_eligible_for_lesson(*, age, gender, lesson):
    if lesson is None:
        return

    assert_age_gender_eligible(
        age=age,
        gender=gender,
        age_min=lesson.age_min,
        age_max=lesson.age_max,
        allowed_gender=lesson.allowed_gender,
        noun="レッスン",
        action="予約",
        exception_class=ValueError,
        require_missing_fields=True,
    )
