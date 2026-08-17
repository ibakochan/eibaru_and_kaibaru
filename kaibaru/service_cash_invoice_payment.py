import logging

from django.db import transaction
from django.utils import timezone

from .models import Payment
from .billing import resolve_and_apply_subscription_period
from .tasks_emails import send_invoice_paid_email


logger = logging.getLogger(__name__)


NON_STRIPE_METHODS = [
    "cash",
    "manual",
    "bank_transfer",
]


class CashInvoicePaymentService:

    @staticmethod
    def mark_paid(*, invoice):

        if invoice.status == "paid":
            return {
                "success": False,
                "reason": "already_paid",
                "invoice_id": invoice.id,
            }


        subscription = invoice.subscription

        if not subscription:
            raise Exception(
                "Invoice has no subscription"
            )


        if subscription.billing_method not in NON_STRIPE_METHODS:
            raise Exception(
                "Stripe invoices cannot be manually marked paid"
            )


        today = timezone.localtime().date()


        with transaction.atomic():

            # -------------------------
            # Create payment record
            # -------------------------

            Payment.objects.get_or_create(
                invoice=invoice,
                defaults={
                    "club": invoice.club,
                    "method": subscription.billing_method,
                    "amount": invoice.amount_due,
                    "currency": invoice.currency,
                    "status": "succeeded",
                }
            )


            # -------------------------
            # Update subscription period
            # -------------------------

            if (
                invoice.billing_reason
                in [
                    "initial_subscription",
                    "subscription_cycle",
                ]
                and subscription.current_period_end
            ):

                resolve_and_apply_subscription_period(
                    subscription,
                    int(subscription.current_period_end.timestamp()),
                    today,
                )


            # -------------------------
            # Mark invoice paid
            # -------------------------

            invoice.status = "paid"
            invoice.amount_paid = invoice.amount_due

            invoice.save(
                update_fields=[
                    "status",
                    "amount_paid",
                ]
            )


            subscription.status = "active"

            subscription.save(
                update_fields=[
                    "status",
                ]
            )


        # -------------------------
        # Email after commit
        # -------------------------

        try:
            send_invoice_paid_email.delay(
                member_id=invoice.member.id,
                amount=invoice.amount_due,
                items=[
                    invoice.description
                ],
                period_end=subscription.access_until,
                plan_name=(
                    invoice.member_plan_name
                    if hasattr(invoice, "member_plan_name")
                    else ""
                ),
            )

        except Exception:
            logger.exception(
                "Failed queueing invoice paid email invoice=%s",
                invoice.id,
            )


        return {
            "success": True,
            "invoice_id": invoice.id,
        }



class BulkCashInvoicePaymentService:

    @staticmethod
    def mark_paid_bulk(*, invoices):

        result = {
            "paid": [],
            "skipped": [],
            "failed": [],
        }


        for invoice in invoices:

            try:

                response = (
                    CashInvoicePaymentService
                    .mark_paid(
                        invoice=invoice
                    )
                )


                if response["success"]:
                    result["paid"].append(
                        invoice.id
                    )

                else:
                    result["skipped"].append(
                        {
                            "invoice_id": invoice.id,
                            "reason": response.get("reason"),
                        }
                    )


            except Exception as e:

                logger.exception(
                    "Failed marking invoice paid invoice=%s",
                    invoice.id,
                )

                result["failed"].append(
                    {
                        "invoice_id": invoice.id,
                        "error": str(e),
                    }
                )


        return result