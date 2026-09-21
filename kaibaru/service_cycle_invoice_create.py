import logging

from datetime import datetime, timedelta, timezone as dt_timezone

from django.db import transaction, IntegrityError
from django.utils import timezone

from .models import (
    Subscription,
    SubscriptionItem,
    Invoice,
    InvoiceItem,
    TicketGrant,
)
from .pricing import (
    calculate_ticket_expiration,
    get_effective_subscription_price,
)
from .discounts import calculate_discounted_amount
from .billing import (
    get_next_billing_cycle_anchor,
    get_access_until,
)
from .tasks_emails import send_invoice_created_email


logger = logging.getLogger(__name__)


CASH_BILLING_METHODS = [
    "cash",
    "bank_transfer",
    "manual",
]


class CashSubscriptionCycleInvoiceService:

    @staticmethod
    def create(*, subscription_id):

        today = timezone.localdate()

        try:
            with transaction.atomic():

                # -----------------------------------------------------
                # 1. Lock subscription
                # -----------------------------------------------------

                subscription = (
                    Subscription.objects
                    .select_for_update()
                    .select_related(
                        "club",
                        "owner",
                    )
                    .get(id=subscription_id)
                )

                # -----------------------------------------------------
                # 2. Safety checks
                # -----------------------------------------------------

                if subscription.billing_method not in CASH_BILLING_METHODS:
                    logger.info(
                        "[CASH BILLING] Skipping subscription=%s "
                        "billing_method=%s",
                        subscription.id,
                        subscription.billing_method,
                    )

                    return {
                        "success": True,
                        "skipped": True,
                        "reason": "not_cash_billing",
                    }

                if subscription.status != "active":
                    logger.info(
                        "[CASH BILLING] Skipping subscription=%s "
                        "status=%s",
                        subscription.id,
                        subscription.status,
                    )

                    return {
                        "success": True,
                        "skipped": True,
                        "reason": "subscription_not_active",
                    }

                if not subscription.current_period_end:
                    logger.warning(
                        "[CASH BILLING] Subscription=%s has no "
                        "current_period_end",
                        subscription.id,
                    )

                    return {
                        "success": False,
                        "skipped": True,
                        "reason": "missing_current_period_end",
                    }

                # -----------------------------------------------------
                # 3. Determine billing cycle
                # -----------------------------------------------------

                billing_cycle_start = timezone.localtime(
                    subscription.current_period_end
                ).date()

                if billing_cycle_start > today:
                    logger.debug(
                        "[CASH BILLING] Subscription=%s not due. "
                        "cycle=%s today=%s",
                        subscription.id,
                        billing_cycle_start,
                        today,
                    )

                    return {
                        "success": True,
                        "skipped": True,
                        "reason": "not_due",
                    }

                billing_cycle_key = billing_cycle_start.isoformat()

                # -----------------------------------------------------
                # 4. Idempotency check
                # -----------------------------------------------------

                existing_invoice = (
                    Invoice.objects
                    .filter(
                        subscription=subscription,
                        billing_reason="subscription_cycle",
                        billing_cycle_key=billing_cycle_key,
                    )
                    .first()
                )

                if existing_invoice:
                    logger.info(
                        "[CASH BILLING] Invoice already exists. "
                        "subscription=%s invoice=%s cycle=%s",
                        subscription.id,
                        existing_invoice.id,
                        billing_cycle_key,
                    )

                    return {
                        "success": True,
                        "already_exists": True,
                        "invoice_id": existing_invoice.id,
                        "amount_due": existing_invoice.amount_due,
                        "billing_cycle_key": billing_cycle_key,
                    }

                # -----------------------------------------------------
                # 5. Load active subscription items
                # -----------------------------------------------------

                subscription_items = list(
                    SubscriptionItem.objects
                    .filter(
                        subscription=subscription,
                        deleted_at__isnull=True,
                    )
                    .select_related(
                        "member",
                        "plan",
                        "plan__ticket_type",
                    )
                    .order_by("id")
                )

                if not subscription_items:
                    logger.warning(
                        "[CASH BILLING] Subscription=%s has no "
                        "active subscription items",
                        subscription.id,
                    )

                    return {
                        "success": False,
                        "skipped": True,
                        "reason": "no_active_items",
                    }

                # -----------------------------------------------------
                # 6. Calculate invoice items
                # -----------------------------------------------------

                calculated_items = []
                total = 0

                for subscription_item in subscription_items:

                    member = subscription_item.member
                    plan = subscription_item.plan

                    if not member:
                        logger.warning(
                            "[CASH BILLING] SubscriptionItem=%s "
                            "has no member",
                            subscription_item.id,
                        )
                        continue

                    if not plan:
                        logger.warning(
                            "[CASH BILLING] SubscriptionItem=%s "
                            "has no plan",
                            subscription_item.id,
                        )
                        continue

                    base = get_effective_subscription_price(
                        subscription_item
                    )

                    discounted = calculate_discounted_amount(
                        club=subscription.club,
                        member=member,
                        plan=plan,
                        base_amount=base,
                        apply_to="subscription",
                    )

                    amount = max(0, int(discounted))

                    if amount <= 0:
                        continue

                    calculated_items.append(
                        {
                            "member": member,
                            "plan": plan,
                            "amount": amount,
                            "description": (
                                f"{member.full_name} "
                                f"{plan.name}"
                            ),
                        }
                    )

                    total += amount

                total = max(0, int(total))

                # -----------------------------------------------------
                # 7. Create local invoice
                # -----------------------------------------------------

                invoice = Invoice.objects.create(
                    club=subscription.club,
                    mutation=None,

                    payer=subscription.owner,
                    payer_name=(
                        subscription.owner.get_full_name()
                        if subscription.owner
                        else None
                    ),
                    payer_email=(
                        subscription.owner.email
                        if subscription.owner
                        else None
                    ),

                    subscription=subscription,

                    status="open",

                    amount_due=total,
                    amount_paid=0,

                    currency="jpy",

                    due_date=subscription.current_period_end,

                    stripe_invoice_id=None,

                    billing_reason="subscription_cycle",

                    billing_cycle_key=billing_cycle_key,
                )

                # -----------------------------------------------------
                # 8. Create InvoiceItems
                # -----------------------------------------------------

                invoice_items = [
                    InvoiceItem(
                        invoice=invoice,
                        member=item["member"],
                        description=item["description"],
                        amount=item["amount"],
                        quantity=1,
                    )
                    for item in calculated_items
                ]

                if invoice_items:
                    InvoiceItem.objects.bulk_create(
                        invoice_items,
                        batch_size=500,
                    )

                # -----------------------------------------------------
                # 9. Grant full-cycle tickets
                # -----------------------------------------------------

                ticket_grant_count = 0
                granted_at = timezone.now()

                for subscription_item in subscription_items:
                    member = subscription_item.member
                    plan = subscription_item.plan

                    if (
                        not member
                        or not plan
                        or plan.plan_type != "ticket_plan"
                        or not plan.ticket_type_id
                        or not plan.ticket_quantity
                    ):
                        continue

                    TicketGrant.objects.create(
                        member=member,
                        ticket_type=plan.ticket_type,
                        source=TicketGrant.Source.SUBSCRIPTION,
                        quantity=plan.ticket_quantity,
                        expires_at=calculate_ticket_expiration(
                            plan=plan,
                            granted_at=granted_at,
                        ),
                    )
                    ticket_grant_count += 1

                # -----------------------------------------------------
                # 10. Calculate NEXT subscription period
                # -----------------------------------------------------

                # billing_cycle_start is the period that was just invoiced.
                #
                # We add one day because get_next_billing_cycle_anchor()
                # treats the anchor day itself as the current month's anchor.
                #
                # Example:
                #
                # current period end = Sep 6
                # reference date      = Sep 7
                # next anchor         = Oct 6

                next_anchor_reference = (
                    billing_cycle_start + timedelta(days=1)
                )

                next_period_end_ts = get_next_billing_cycle_anchor(
                    next_anchor_reference,
                    subscription.billing_anchor_day,
                )

                if not next_period_end_ts:
                    logger.error(
                        "[CASH BILLING] Could not calculate next period "
                        "for subscription=%s anchor_day=%s",
                        subscription.id,
                        subscription.billing_anchor_day,
                    )

                    raise ValueError(
                        "Could not calculate next billing cycle anchor"
                    )

                next_period_end = datetime.fromtimestamp(
                    next_period_end_ts,
                    tz=dt_timezone.utc,
                )

                # -----------------------------------------------------
                # 11. Calculate access_until
                # -----------------------------------------------------

                next_access_until = get_access_until(
                    next_period_end,
                    subscription.billing_mode,
                )

                if not next_access_until:
                    logger.error(
                        "[CASH BILLING] Could not calculate access_until "
                        "for subscription=%s billing_mode=%s",
                        subscription.id,
                        subscription.billing_mode,
                    )

                    raise ValueError(
                        "Could not calculate subscription access_until"
                    )

                # -----------------------------------------------------
                # 12. Advance subscription entitlement
                # -----------------------------------------------------

                subscription.current_period_end = next_period_end
                subscription.access_until = next_access_until

                subscription.save(
                    update_fields=[
                        "current_period_end",
                        "access_until",
                    ]
                )

                # -----------------------------------------------------
                # 13. Email only after successful DB commit
                # -----------------------------------------------------

                transaction.on_commit(
                    lambda invoice_id=invoice.id:
                        send_invoice_created_email.delay(invoice_id)
                )

                logger.info(
                    "[CASH BILLING] Created invoice and advanced "
                    "subscription: invoice=%s subscription=%s "
                    "cycle=%s next_period_end=%s access_until=%s "
                    "amount=%s items=%s",
                    invoice.id,
                    subscription.id,
                    billing_cycle_key,
                    subscription.current_period_end,
                    subscription.access_until,
                    total,
                    len(invoice_items),
                )

                return {
                    "success": True,
                    "invoice_id": invoice.id,
                    "subscription_id": subscription.id,
                    "amount_due": total,
                    "invoice_item_count": len(invoice_items),
                    "billing_cycle_key": billing_cycle_key,
                    "ticket_grant_count": ticket_grant_count,
                    "current_period_end": (
                        subscription.current_period_end.date()
                    ),
                    "access_until": (
                        subscription.access_until.date()
                        if subscription.access_until
                        else None
                    ),
                }

        except IntegrityError:

            # ---------------------------------------------------------
            # Database uniqueness is the final idempotency guarantee.
            # ---------------------------------------------------------

            logger.info(
                "[CASH BILLING] Duplicate invoice prevented by "
                "database constraint subscription=%s cycle=%s",
                subscription_id,
                billing_cycle_key,
            )

            invoice = (
                Invoice.objects
                .filter(
                    subscription_id=subscription_id,
                    billing_reason="subscription_cycle",
                    billing_cycle_key=billing_cycle_key,
                )
                .first()
            )

            return {
                "success": True,
                "already_exists": True,
                "invoice_id": invoice.id if invoice else None,
            }