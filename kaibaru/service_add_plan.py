import stripe
from django.core.cache import cache
from django.utils import timezone

from .models import SubscriptionItem, SubscriptionMutation, Invoice, InvoiceItem, Payment, Member
from django.db import transaction
from .pricing import calculate_joining_fee, calculate_subscription_pricing
from .discounts import (
    calculate_discounted_amount,
)
from .stripe_service import get_or_create_stripe_customer

from .service_mutations import (
    assert_mutation_not_locked,
    stripe_idempotency_key,
    get_or_create_mutation_strict,
)


class SubscriptionAddPlanService:

    @staticmethod
    def add_plan_to_existing_subscription(
        *,
        club,
        member,
        plan,
        subscription,
    ):
        """
        PURE EXTRACTION of existing 'sub exists' add-plan logic.
        NO BEHAVIOR CHANGES.
        """

        today = timezone.localtime().date()
        billing_user = member.owner

        stripe_customer_obj = get_or_create_stripe_customer(billing_user, club)

        # =========================================================
        # CALCULATE ADD PLAN BILLING COMPONENTS
        # BEFORE ANY STRIPE INVOICE ITEM CREATION
        # =========================================================
        
        expected_invoice_items = {}
        
        joining_fee_amount = 0
        proration_amount = 0
        next_month_amount = 0
        proration_remaining_days = 0
        
        pricing = calculate_subscription_pricing(
            club=club,
            member=member,
            plan=plan,
            plan_price=plan.price,
            today=today,
            mode=subscription.billing_mode,
            anchor_day=subscription.billing_anchor_day,
        )
        
        
        # -------------------------
        # joining fee
        # -------------------------
        if (
            club.joining_fee > 0
            and not member.has_been_charged_joining_fee
        ):
        
            joining_fee_amount = calculate_joining_fee(
                club,
                member,
            )
        
            if joining_fee_amount > 0:
                expected_invoice_items["joining_fee"] = {
                    "amount": joining_fee_amount,
                }
        
        
        # -------------------------
        # proration
        # -------------------------
        proration_amount = pricing["final_amount"]
        
        if proration_amount > 0:
            expected_invoice_items["prorations"] = {
                "amount": proration_amount,
                "remaining_days": pricing["proration"]["remaining_days"],
            }
        
        
        # -------------------------
        # next month prepaid
        # monthly mode only
        # -------------------------
        if subscription.billing_mode == "monthly":
        
            anchor_day = subscription.billing_anchor_day
        
            if today.day > anchor_day:
        
                next_month_amount = calculate_discounted_amount(
                    club=club,
                    member=member,
                    plan=plan,
                    base_amount=plan.price,
                    apply_to="subscription",
                )
        
                expected_invoice_items["next_month_fee"] = {
                    "amount": next_month_amount,
                    "description": f"{plan.name} 翌月分前払い",
                }
        
        
        mutation, created = get_or_create_mutation_strict(
            subscription=subscription,
            item=None,
            mutation_type=SubscriptionMutation.MutationType.ADD_PLAN,
            mutation_key=f"add_plan_member_{member.id}_plan_{plan.id}",
            invoice_status=SubscriptionMutation.InvoiceStatus.NOT_STARTED,
            payload={
                "mutation_key": f"add_plan_member_{member.id}_plan_{plan.id}",
                "member_id": member.id,
                "plan_id": plan.id,
                "stripe_price_id": plan.stripe_price_id,
                "expected_invoice_items": expected_invoice_items,
            },
        )

        if created:
            proration_remaining_days = (
                pricing["proration"]["remaining_days"]
            )

        else:
            # overwrite calculated variables with frozen payload values
        
            expected_invoice_items = (
                mutation.payload.get("expected_invoice_items", {})
            )

            if expected_invoice_items is None:
                raise Exception(
                    "ADD_PLAN mutation missing frozen invoice payload"
                )
        
            joining_fee_amount = (
                expected_invoice_items
                .get("joining_fee", {})
                .get("amount", 0)
            )
        
            proration_amount = (
                expected_invoice_items
                .get("prorations", {})
                .get("amount", 0)
            )

            next_month_amount = (
                expected_invoice_items
                .get("next_month_fee", {})
                .get("amount", 0)
            )

            proration_remaining_days = (
                expected_invoice_items
                .get("prorations", {})
                .get("remaining_days")
            )
        
        

        
        # =========================================================
        # REGULAR MODE
        # =========================================================

        invoice = stripe.Invoice.create(
            customer=stripe_customer_obj.stripe_customer_id,
            subscription=subscription.stripe_subscription_id,
            auto_advance=False,
            metadata={
                "mutation_id": str(mutation.id),
                "type": "add_plan",
                "member_id": str(member.id),
                "plan_id": str(plan.id),
            },
            stripe_account=club.stripe_account_id,
            idempotency_key=stripe_idempotency_key(
                mutation,
                "create_invoice"
            ),
        )
        if subscription.billing_mode == "regular":



            # -------------------------
            # joining fee
            # -------------------------
            if joining_fee_amount > 0:


                if joining_fee_amount > 0:
                    stripe.InvoiceItem.create(
                        customer=stripe_customer_obj.stripe_customer_id,
                        invoice=invoice.id,
                        amount=joining_fee_amount,
                        currency="jpy",
                        description=f"{member.full_name} 入会金",
                        metadata={
                            "mutation_id": str(mutation.id),
                            "member_id": member.id,
                            "club_id": club.id,
                            "plan_id": plan.id,
                            "type": "joining_fee",
                        },
                        stripe_account=club.stripe_account_id,
                        idempotency_key=stripe_idempotency_key(
                            mutation,
                            "create_joining_fee_invoice_item"
                        ),
                    )





            if proration_amount > 0:
                stripe.InvoiceItem.create(
                    customer=stripe_customer_obj.stripe_customer_id,
                    invoice=invoice.id,
                    amount=proration_amount,
                    currency="jpy",
                    description=(
                        f"Prorated membership "
                        f"({proration_remaining_days} days until next anchor)"
                    ),
                    metadata={
                        "mutation_id": str(mutation.id),
                        "member_id": member.id,
                        "club_id": club.id,
                        "plan_id": plan.id,
                        "type": "prorations",
                    },
                    stripe_account=club.stripe_account_id,
                    idempotency_key=stripe_idempotency_key(
                        mutation,
                        "create_proration_invoice_item"
                    ),
                )

        # =========================================================
        # MONTHLY MODE
        # =========================================================
        else:

            anchor_day = subscription.billing_anchor_day



            # joining fee
            if joining_fee_amount > 0:


                if joining_fee_amount > 0:
                    stripe.InvoiceItem.create(
                        customer=stripe_customer_obj.stripe_customer_id,
                        invoice=invoice.id,
                        amount=joining_fee_amount,
                        currency="jpy",
                        description=f"{member.full_name} 入会金",
                        metadata={
                            "mutation_id": str(mutation.id),
                            "member_id": member.id,
                            "club_id": club.id,
                            "plan_id": plan.id,
                            "type": "joining_fee",
                        },
                        stripe_account=club.stripe_account_id,
                        idempotency_key=stripe_idempotency_key(
                            mutation,
                            "create_joining_fee_invoice_item"
                        ),
                    )





            if proration_amount > 0:
                stripe.InvoiceItem.create(
                    customer=stripe_customer_obj.stripe_customer_id,
                    invoice=invoice.id,
                    amount=proration_amount,
                    currency="jpy",
                    description=(
                        f"Prorated membership "
                        f"({proration_remaining_days} days until next anchor)"
                    ),
                    metadata={
                        "mutation_id": str(mutation.id),
                        "member_id": member.id,
                        "club_id": club.id,
                        "plan_id": plan.id,
                        "type": "prorations",
                    },
                    stripe_account=club.stripe_account_id,
                    idempotency_key=stripe_idempotency_key(
                        mutation,
                        "create_proration_invoice_item"
                    ),
                )


            if next_month_amount > 0:
                stripe.InvoiceItem.create(
                    customer=stripe_customer_obj.stripe_customer_id,
                    invoice=invoice.id,
                    amount=next_month_amount,
                    currency="jpy",
                    description=f"{plan.name} 翌月分前払い",
                    metadata={
                        "mutation_id": str(mutation.id),
                        "member_id": member.id,
                        "club_id": club.id,
                        "plan_id": plan.id,
                        "type": "next_month_fee",
                    },
                    stripe_account=club.stripe_account_id,
                    idempotency_key=stripe_idempotency_key(
                        mutation,
                        "create_next_month_fee_invoice_item"
                    ),
                )

        
        mutation.payload["invoice_id"] = invoice.id

        mutation.save(
            update_fields=[
                "payload",
            ]
        )

        invoice = stripe.Invoice.finalize_invoice(
            invoice.id,
            stripe_account=club.stripe_account_id,
        )

        if invoice.amount_due > 0:
            invoice = stripe.Invoice.pay(
                invoice.id,
                stripe_account=club.stripe_account_id,
                idempotency_key=stripe_idempotency_key(
                    mutation,
                    "pay_invoice"
                ),
            )

        invoice = stripe.Invoice.retrieve(
            invoice.id,
            expand=["lines.data"],
            stripe_account=club.stripe_account_id,
        )

        if invoice.status != "paid":

            try:
                stripe.Invoice.void_invoice(
                    invoice.id,
                    stripe_account=club.stripe_account_id,
                )

            except stripe.error.InvalidRequestError:
                # Could have become paid between retrieve and void
                pass


            invoice = stripe.Invoice.retrieve(
                invoice.id,
                expand=["lines.data"],
                stripe_account=club.stripe_account_id,
            )


        if invoice.status != "paid":

            mutation.invoice_status = (
                SubscriptionMutation.InvoiceStatus.FAILED
            )
    
            mutation.status = (
                SubscriptionMutation.Status.FAILED
            )
    
            mutation.payload["failure_reason"] = (
                f"Invoice not paid. Final status: {invoice.status}"
            )

            mutation.save(
                update_fields=[
                    "invoice_status",
                    "status",
                    "payload",
                ]
            )
    
            return {
                "success": False,
                "message": "Invoice payment failed"
            }
       
        if invoice.status == "paid":
            local_invoice, created = Invoice.objects.get_or_create(
                stripe_invoice_id=invoice.id,
                defaults={
                    "club": club,
                    "mutation": mutation,
                    "payer": subscription.owner,
                    "payer_name": subscription.owner.get_full_name(),
                    "payer_email": subscription.owner.email,
                    "subscription": subscription,
                    "status": "paid",
                    "billing_reason": "add_plan",
                    "amount_due": invoice.amount_due,
                    "amount_paid": invoice.amount_paid,
                    "currency": "jpy",
                }
            )
    
            if created:
                for line in invoice.lines.data:
    
                    metadata = line.metadata or {}
            
                    member_id = metadata.get("member_id")
            
                    member_obj = None
            
                    if member_id:
                        member_obj = Member.objects.filter(
                            id=member_id
                        ).first()
            
                    InvoiceItem.objects.create(
                        invoice=local_invoice,
                        member=member_obj,
                        description=line.description,
                        amount=line.amount,
                        quantity=1,
                    )
    
            Payment.objects.get_or_create(
                invoice=local_invoice,
                defaults={
                    "club": club,
                    "method": "stripe",
                    "amount": invoice.amount_paid,
                    "currency": "jpy",
                    "status": "succeeded",
                    "paid_at": timezone.now(),
                }
            )        

            mutation.invoice_status = (
                SubscriptionMutation.InvoiceStatus.PAID
            )

        mutation.save(
            update_fields=[
                "invoice_status",
            ]
        )
        

        


        sub_data = stripe.Subscription.retrieve(
            subscription.stripe_subscription_id,
            expand=["items.data"],
            stripe_account=club.stripe_account_id
        )

        if subscription.billing_mode == "regular":

            

            existing_item = next(
                (i for i in sub_data["items"]["data"]
                 if i["price"]["id"] == plan.stripe_price_id),
                None
            )

            active_qty = SubscriptionItem.objects.filter(
                subscription=subscription,
                stripe_price_id_at_subscription=plan.stripe_price_id,
                deleted_at__isnull=True
            ).count()

            desired_qty = active_qty + 1

            if existing_item:
                stripe_item_id = existing_item["id"]

                stripe.SubscriptionItem.modify(
                    stripe_item_id,
                    quantity=desired_qty,
                    proration_behavior="none",
                    stripe_account=club.stripe_account_id,
                    idempotency_key=stripe_idempotency_key(
                        mutation,
                        "add_plan_modify_quantity"
                    ),
                )
            else:
                stripe_item = stripe.SubscriptionItem.create(
                    subscription=subscription.stripe_subscription_id,
                    price=plan.stripe_price_id,
                    quantity=1,
                    proration_behavior="none",
                    stripe_account=club.stripe_account_id,
                    idempotency_key=stripe_idempotency_key(
                        mutation,
                        "add_plan_create_item"
                    ),
                )
                stripe_item_id = stripe_item.id


        # =========================================================
        # MONTHLY MODE
        # =========================================================
        else:

            anchor_day = subscription.billing_anchor_day

            

            existing_item = next(
                (i for i in sub_data["items"]["data"]
                 if i["price"]["id"] == plan.stripe_price_id),
                None
            )

            active_qty = SubscriptionItem.objects.filter(
                subscription=subscription,
                stripe_price_id_at_subscription=plan.stripe_price_id,
                deleted_at__isnull=True
            ).count()

            desired_qty = active_qty + 1

            if existing_item:
                stripe_item_id = existing_item["id"]

                stripe.SubscriptionItem.modify(
                    stripe_item_id,
                    quantity=desired_qty,
                    proration_behavior="none",
                    stripe_account=club.stripe_account_id,
                    idempotency_key=stripe_idempotency_key(
                        mutation,
                        "add_plan_modify_quantity"
                    ),
                )
            else:
                stripe_item = stripe.SubscriptionItem.create(
                    subscription=subscription.stripe_subscription_id,
                    price=plan.stripe_price_id,
                    quantity=1,
                    proration_behavior="none",
                    stripe_account=club.stripe_account_id,
                    idempotency_key=stripe_idempotency_key(
                        mutation,
                        "add_plan_create_item"
                    ),
                )
                stripe_item_id = stripe_item.id





        if subscription.cancel_at_period_end:
    
            fresh_sub = stripe.Subscription.retrieve(
                subscription.stripe_subscription_id,
                expand=["items.data"],
                stripe_account=club.stripe_account_id
            )
    
            active_price_ids = set(
                SubscriptionItem.objects.filter(
                subscription=subscription,
                deleted_at__isnull=True
                ).values_list("stripe_price_id_at_subscription", flat=True)
            )
    
            active_price_ids.add(plan.stripe_price_id)
    
            for stripe_item in fresh_sub["items"]["data"]:
                if stripe_item["price"]["id"] not in active_price_ids:
                    stripe.SubscriptionItem.delete(
                        stripe_item["id"],
                        proration_behavior="none",
                        stripe_account=club.stripe_account_id,
                        idempotency_key=stripe_idempotency_key(
                            mutation,
                            "cleanup_remove_cancelled_item"
                        )
                    )
    
            stripe.Subscription.modify(
                subscription.stripe_subscription_id,
                cancel_at_period_end=False,
                stripe_account=club.stripe_account_id
            )


        # =========================================================
        # DB UPDATE (UNCHANGED)
        # =========================================================

        with transaction.atomic():
            item = SubscriptionItem.objects.filter(
                subscription=subscription,
                member=member,
                plan=plan,
            ).first()
    
            if item:
                item.deleted_at = None
                item.price_at_subscription = plan.price
                item.stripe_price_id_at_subscription = plan.stripe_price_id
                item.save()
    
            else:
                SubscriptionItem.objects.create(
                    member=member,
                    subscription=subscription,
                    plan=plan,
                    stripe_subscription_item_id=stripe_item_id,
                    price_at_subscription=plan.price,
                    stripe_price_id_at_subscription=plan.stripe_price_id,
                )

            if joining_fee_amount > 0:
                member.has_been_charged_joining_fee = True
                member.save(
                    update_fields=[
                        "has_been_charged_joining_fee"
                    ]
                )
    
            # =========================================================
            # CLEANUP (UNCHANGED)
            # =========================================================
            if subscription.cancel_at_period_end:
    
                subscription.cancel_at_period_end = False
                subscription.save(update_fields=["cancel_at_period_end"])
            
            mutation.status = SubscriptionMutation.Status.SUCCEEDED
            mutation.processed_at = timezone.now()
    
            mutation.save(
                update_fields=[
                    "status",
                    "processed_at",
                ]
            )
    
        return {
            "success": True,
            "message": "Plan added to existing subscription"
        }