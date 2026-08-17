from django.db import transaction
from django.utils import timezone

from .models import SubscriptionMutation


class MutationLockedError(Exception):
    def __init__(self, blocked_until):
        self.blocked_until = blocked_until

        message = (
            f"この操作は現在ロックされています。"
            f"{blocked_until} 以降に再度お試しください。"
        )
        super().__init__(message)

def assert_mutation_not_locked(*, item, mutation_type):
    now = timezone.now()

    locked = SubscriptionMutation.objects.filter(
        item=item,
        type=mutation_type,
        status=SubscriptionMutation.Status.SUCCEEDED,
        secondary_mutation_blocked_until__gt=now
    ).first()

    if locked:
        raise MutationLockedError(locked.secondary_mutation_blocked_until)

def stripe_idempotency_key(mutation, action):
    return f"mutation_{mutation.id}_{action}"


ACTIVE_STATUSES = [
    SubscriptionMutation.Status.PENDING,
    SubscriptionMutation.Status.PROCESSING,
]


def get_or_create_mutation_strict(*, subscription, item, mutation_type, payload=None, mutation_key=None, invoice_status=None):
    with transaction.atomic():
        if mutation_key:
            existing = (
                SubscriptionMutation.objects
                .select_for_update()
                .filter(
                    subscription=subscription,
                    type=mutation_type,
                    payload__mutation_key=mutation_key,
                    status__in=ACTIVE_STATUSES,
                )
                .order_by("-created_at")
                .first()
            )

        else:
            existing = (
                SubscriptionMutation.objects
                .select_for_update()
                .filter(
                    subscription=subscription,
                    item=item,
                    type=mutation_type,
                    status__in=ACTIVE_STATUSES,
                )
                .order_by("-created_at")
                .first()
            )

        if existing:
            # ✔ same exact action → allow reuse (idempotent retry)
            if existing.type == mutation_type:
                return existing, False

            # ❌ same item/subscription but different action → BLOCK HARD
            raise Exception(
                f"Conflicting mutation in progress: "
                f"{existing.type} vs {mutation_type}"
            )

        mutation = SubscriptionMutation.objects.create(
            subscription=subscription,
            item=item,
            type=mutation_type,
            payload=payload or {},
            status=SubscriptionMutation.Status.PROCESSING,
            invoice_status=invoice_status,
        )

        return mutation, True