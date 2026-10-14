from datetime import timedelta


UNSUBSCRIBED_OPEN_DAYS = 7
UNSUBSCRIBED_DELETE_DAYS = 30

MEMBERS_REQUIRE_SUBSCRIPTION = (
    "プラットフォームのサブスクリプションに登録するまで会員を追加できません。"
)
CONNECT_REQUIRES_SUBSCRIPTION = (
    "先にKaibaruのサブスクリプションに登録してください。"
)
FIRST_OF_MONTH_BLOCKED = (
    "1日はサブスクリプションの登録ができません。2日以降にお試しください。"
)


def unsubscribed_open_days(created_on):
    """Days a new club stays unfrozen before it has a platform subscription.

    The window is 7 days, counting the creation day. If the 1st falls in
    those 7 days, it becomes 8, because checkout is closed on the 1st.
    """
    for offset in range(UNSUBSCRIBED_OPEN_DAYS):
        if (created_on + timedelta(days=offset)).day == 1:
            return UNSUBSCRIBED_OPEN_DAYS + 1
    return UNSUBSCRIBED_OPEN_DAYS


def club_has_platform_subscription(club):
    return bool(getattr(club, "stripe_subscription_id", None))
