MIN_PAID_AMOUNT = 50

FREE_OR_MIN_MESSAGE = "料金は0円、または50円以上にしてください。"
MIN_ONLY_MESSAGE = "料金は50円以上にしてください。"
SUBSCRIPTION_SMALL_AMOUNT_MESSAGE = "1円から49円の月額は50円に繰り上げられます。"


def normalize_paid_amount(amount, *, allow_free=True):
    """0 or below stays free. 1-49 is charged as 50."""
    try:
        amount = int(amount or 0)
    except (TypeError, ValueError):
        amount = 0

    if amount <= 0:
        return 0 if allow_free else MIN_PAID_AMOUNT
    if amount < MIN_PAID_AMOUNT:
        return MIN_PAID_AMOUNT
    return amount


def invalid_set_price(amount, *, allow_free=True):
    """Reject amounts that are not free and not at least the Stripe minimum."""
    if amount is None:
        return None
    try:
        amount = int(amount)
    except (TypeError, ValueError):
        return FREE_OR_MIN_MESSAGE if allow_free else MIN_ONLY_MESSAGE
    if amount < 0:
        return FREE_OR_MIN_MESSAGE if allow_free else MIN_ONLY_MESSAGE
    if amount == 0:
        return None if allow_free else MIN_ONLY_MESSAGE
    if amount < MIN_PAID_AMOUNT:
        return FREE_OR_MIN_MESSAGE if allow_free else MIN_ONLY_MESSAGE
    return None


def persisted_charge_amount(instance, field="amount"):
    """Round a stored charge up to the paid minimum before Stripe sees it."""
    current = getattr(instance, field)
    charged = normalize_paid_amount(current, allow_free=True)
    try:
        stored = int(current or 0)
    except (TypeError, ValueError):
        stored = 0
    if charged != stored:
        setattr(instance, field, charged)
        instance.save(update_fields=[field])
    return charged
