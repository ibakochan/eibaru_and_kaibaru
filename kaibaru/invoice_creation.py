from .models import Invoice, InvoiceItem, Payment, Member

from django.db import transaction
from django.utils import timezone

def create_local_invoice_from_stripe_invoice(
    *,
    stripe_invoice,
    subscription,
    billing_reason,
    initial_status="open",
):
    """
    Create the local Invoice, InvoiceItems and Payment for a Stripe invoice.

    This represents the invoice/debt existing locally.
    It does NOT mean the invoice has been paid.
    """

    local_invoice, created = Invoice.objects.get_or_create(
        stripe_invoice_id=stripe_invoice["id"],
        defaults={
            "club": subscription.club,
            "payer": subscription.owner,
            "payer_name": subscription.owner.get_full_name(),
            "payer_email": subscription.owner.email,
            "subscription": subscription,
            "status": initial_status,
            "billing_reason": billing_reason,
            "amount_due": stripe_invoice.get("amount_due", 0),
            "amount_paid": (
                stripe_invoice.get("amount_paid", 0)
                if initial_status == "paid"
                else 0
            ),
            "currency": stripe_invoice.get("currency", "jpy"),
        },
    )

    # ---------------------------------------------------------
    # InvoiceItems
    # ---------------------------------------------------------
    if created:
        for line in stripe_invoice.get("lines", {}).get("data", []):
            metadata = line.get("metadata", {})

            member = None

            member_id = metadata.get("member_id")
            if member_id:
                member = Member.objects.filter(id=member_id).first()

            InvoiceItem.objects.create(
                invoice=local_invoice,
                member=member,
                description=line.get("description", ""),
                amount=line.get("amount", 0),
                quantity=line.get("quantity", 1),
            )

    # ---------------------------------------------------------
    # Payment
    # ---------------------------------------------------------
    payment, payment_created = Payment.objects.get_or_create(
        invoice=local_invoice,
        defaults={
            "club": subscription.club,
            "method": "stripe",
            "amount": (
                stripe_invoice.get("amount_paid", 0)
                if initial_status == "paid"
                else stripe_invoice.get("amount_due", 0)
            ),
            "currency": stripe_invoice.get("currency", "jpy"),
            "status": (
                "succeeded"
                if initial_status == "paid"
                else "pending"
            ),
            "paid_at": timezone.now() if initial_status == "paid" else None,
        },
    )

    return local_invoice, payment


def mark_local_invoice_paid(
    *,
    local_invoice,
    stripe_invoice,
):
    """
    Mark an existing local invoice/payment as paid.

    Stripe's invoice.paid event is the authority for this transition.
    """

    with transaction.atomic():
        local_invoice.status = "paid"
        local_invoice.amount_due = stripe_invoice.get(
            "amount_due",
            local_invoice.amount_due,
        )
        local_invoice.amount_paid = stripe_invoice.get(
            "amount_paid",
            local_invoice.amount_paid,
        )
        local_invoice.currency = stripe_invoice.get(
            "currency",
            local_invoice.currency,
        )

        local_invoice.save(
            update_fields=[
                "status",
                "amount_due",
                "amount_paid",
                "currency",
            ]
        )

        payment, created = Payment.objects.get_or_create(
            invoice=local_invoice,
            defaults={
                "club": local_invoice.club,
                "method": "stripe",
                "amount": stripe_invoice.get("amount_paid", 0),
                "currency": stripe_invoice.get("currency", "jpy"),
                "status": "succeeded",
                "paid_at": timezone.now(),
            },
        )

        if not created:
            payment.method = "stripe"
            payment.amount = stripe_invoice.get(
                "amount_paid",
                payment.amount,
            )
            payment.currency = stripe_invoice.get(
                "currency",
                payment.currency,
            )
            payment.status = "succeeded"
            payment.paid_at = payment.paid_at or timezone.now()

            payment.save(
                update_fields=[
                    "method",
                    "amount",
                    "currency",
                    "status",
                    "paid_at",
                ]
            )

    return local_invoice, payment