from django.utils import timezone
from rest_framework import viewsets, serializers, status
from rest_framework.decorators import action
from rest_framework.viewsets import ViewSet
from rest_framework.response import Response
from .models import TicketGrant, TicketType, TicketPackage, Reservation, Event, Invoice, MemberPricingAdjustment, Discount, DiscountCondition, SubscriptionItem, Member, Club, Lesson, Participation, SlateImage, JoinRequest, MembershipPlan, Subscription
from accounts.models import CustomUser
from django.contrib.auth import login
import re
import unicodedata
import json
import logging
from django.db.models import Q, F
from datetime import date
from django.db.models import Sum
from .service_legacy_subscription import LegacySubscriptionService


from collections import defaultdict

import stripe
from django.conf import settings
from django.db.models import Prefetch
from django.core.exceptions import ValidationError
from .user_errors import format_validation_error
stripe.api_key = settings.STRIPE_SECRET_KEY

from .tasks import delete_membership_plan_task

from .discounts import calculate_discounted_amount
import uuid

from .pricing import (
    calculate_joining_fee,
    calculate_subscription_pricing,
    get_effective_subscription_price,
)

from .serializers import TicketTypeSerializer, TicketPackageSerializer, ReservationSerializer, PaymentHistoryInvoiceSerializer, MemberPricingAdjustmentSerializer, DiscountConditionSerializer, DiscountSerializer, MembershipPlanSerializer, MemberSerializer, ClubSerializer, LessonSerializer, ParticipationSerializer, SlateImageSerializer, JoinRequestSerializer
from .service_event import save_section_event
from django.conf import settings
from datetime import timedelta
import hashlib

from django.db import transaction

from .utils import sync_member_quantity
from .rules_plans import would_break_any_bundle

from .rules_subscriptions import validate_plan_set

from django.core.files.base import ContentFile
import os
from django.utils.timezone import now
class PreviewMember:

    def __init__(
        self,
        gender=None,
        age=None,
        family_count=None,
    ):
        self.id = -1
        self.is_preview = True
        self.gender = gender
        self.owner_id = None
        self.owner = None
        self.has_been_charged_joining_fee = False
        self.has_paid_joining_fee = False

        if age not in [None, ""]:
            today = date.today()
            self.birth_date = date(
                today.year - int(age),
                today.month,
                today.day
            )
        else:
            self.birth_date = None

        if family_count not in [None, ""]:
            self.family_count = int(family_count)
        else:
            self.family_count = 0


def build_member_adjustment_map(members):
    qs = MemberPricingAdjustment.objects.filter(
        member_id__in=[m.id for m in members],
    ).prefetch_related("plans")

    result = defaultdict(list)

    for adj in qs:
        adj.plan_ids = {p.id for p in adj.plans.all()}  # 👈 add this
        result[adj.member_id].append(adj)

    return result

def build_pricing_map(club, request_user=None, preview_member=None):
    from .billing import compute_access_period_preview
    from collections import defaultdict
    from django.db.models import Q

    from .models import Subscription, SubscriptionItem, Member
    from .pricing import (
        calculate_ticket_proration,
        calculate_regular_proration,
        calculate_monthly_proration,
        calculate_ticket_expiration,
    )
    from .discounts import (
        build_discount_context,
        build_member_discount_map,
        build_discount_plan_ids_map,
        apply_discounts,
        get_applicable_discounts,
    )

    today = timezone.localdate()

    # -------------------------
    # ROLE LOGIC
    # -------------------------
    is_club_owner = (request_user and club.owner_id == request_user.id)
    user_member = Member.objects.filter(club=club, user=request_user).first()
    is_manager = user_member.is_manager if user_member else False
    can_view_all = is_club_owner or is_manager
    is_authenticated = (
        request_user.is_authenticated
        if request_user
        else False
    )

    # -------------------------
    # SHARED DATA FETCHING
    # -------------------------
    if preview_member:
        members = [preview_member]
    else:
        members = list(
            club.members.all().select_related("owner", "club")
        )

    member_adjustments = build_member_adjustment_map(members)

    subs_by_owner = {
        sub.owner_id: sub
        for sub in Subscription.objects.filter(
            club=club,
            status__in=["active", "trialing", "pending"],
        )
    }

    all_items = list(
        SubscriptionItem.objects.filter(
            subscription__club=club,
            subscription__status__in=["active", "trialing", "pending"],
        )
        .select_related("member", "plan", "subscription")
        .filter(
            Q(deleted_at__isnull=True) |
            Q(deleted_at__isnull=False, access_until__gt=timezone.now())
        )
    )

    items_by_member = defaultdict(list)
    for item in all_items:
        items_by_member[item.member_id].append(item)

    ticket_grants = list(
        TicketGrant.objects
        .filter(
            member_id__in=[
                m.id
                for m in members
                if not getattr(m, "is_preview", False)
            ],
        )
        .select_related("ticket_type")
        .filter(
            Q(expires_at__isnull=True)
            | Q(expires_at__gte=timezone.now())
        )
        .order_by("expires_at", "id")
    )
    
    ticket_grants_by_member = defaultdict(list)
    
    for grant in ticket_grants:
        ticket_grants_by_member[grant.member_id].append(grant)
    
    
    
    plans = list(
        club.membership_plans
        .filter(active=True)
        .select_related("ticket_type")
    )

    # -------------------------
    # DISCOUNTS (RAW)
    # -------------------------
    subscription_discounts = get_applicable_discounts(
        club, "subscription"
    ).prefetch_related("plans")

    joining_fee_discounts = get_applicable_discounts(
        club, "joining_fee"
    ).prefetch_related("plans")

    # -------------------------
    # CONTEXT PRECOMPUTE
    # -------------------------
    context = build_discount_context(members)

    member_ages = context["member_ages"]
    family_counts = context["family_counts"]

    # -------------------------
    # DISCOUNT MAPS (MEMBER → DISCOUNTS)
    # -------------------------
    member_subscription_discounts = build_member_discount_map(
        members,
        subscription_discounts,
        member_ages=member_ages,
        family_counts=family_counts,
    )

    member_joining_discounts = build_member_discount_map(
        members,
        joining_fee_discounts,
        member_ages=member_ages,
        family_counts=family_counts,
    )

    # -------------------------
    # PLAN FILTER MAPS
    # -------------------------
    subscription_discount_plan_ids = build_discount_plan_ids_map(
        subscription_discounts
    )

    joining_discount_plan_ids = build_discount_plan_ids_map(
        joining_fee_discounts
    )

    # -------------------------
    # PRICING MAP BUILD
    # -------------------------
    pricing_map = {}

    for member in members:
        if not getattr(member, "is_preview", False):
            if is_authenticated and not can_view_all and member.owner_id != request_user.id:
                continue

        member_data = {}

        sub = subs_by_owner.get(member.owner_id)
        if sub:
            anchor_day = sub.billing_anchor_day
            mode = sub.billing_mode
        else:
            anchor_day = club.stripe_anchor_date
            mode = club.subscription_mode

        access_period = None

        if sub:
            access_until = sub.access_until
            current_period_end = sub.current_period_end

            access_period = {
                "access_start": today,
                "access_until": access_until,
                "current_period_end": current_period_end,
                "days": (access_until.date() - today).days + 1 if access_until else 0,
                "source": "live",
            }
        else:
            access_period = compute_access_period_preview(today, mode, anchor_day)
            access_period["source"] = "preview"
        
        member_data["access_preview"] = {
            "start": access_period["access_start"].isoformat(),
            "end": access_period["access_until"].isoformat() if access_period["access_until"] else None,
            "current_period_end": access_period["current_period_end"].isoformat() if access_period["current_period_end"] else None,
            "days": access_period["days"],

            "billing_mode": mode,
            "billing_anchor_day": anchor_day,
            "source": access_period.get("source"),
        }

        # =========================================================
        # SELF VIEW
        # =========================================================
        if preview_member or (is_authenticated and member.owner_id == request_user.id):

            # -------------------------
            # Joining fee
            # -------------------------
            if club.joining_fee > 0 and not member.has_been_charged_joining_fee:
                member_data["joining_fee"] = apply_discounts(
                    member=member,
                    member_adjustments=member_adjustments,
                    member_id=member.id,
                    base_amount=club.joining_fee,
                    discount_type="joining_fee",
                    member_subscription_discounts=member_subscription_discounts,
                    member_joining_discounts=member_joining_discounts,
                    subscription_discount_plan_ids=subscription_discount_plan_ids,
                    joining_discount_plan_ids=joining_discount_plan_ids,
                )

            # -------------------------
            # Existing items
            # -------------------------
            subscription_items = []
            for item in items_by_member.get(member.id, []):
                plan = item.plan
                if not plan:
                    continue

                ticket_data = None

                if plan.plan_type == "ticket_plan":

                    grants = [
                        grant
                        for grant in ticket_grants_by_member.get(member.id, [])
                        if grant.ticket_type_id == plan.ticket_type_id
                    ]
                
                    ticket_data = {
                        "ticket_type_id": plan.ticket_type_id,
                        "ticket_type_name": (
                            plan.ticket_type.name
                            if plan.ticket_type
                            else None
                        ),
                        "monthly_quantity": plan.ticket_quantity,
                        "expiration_mode": plan.ticket_expiration_mode,
                        "expiration_days": plan.ticket_expiration_days,
                        "grants": [
                            {
                                "id": grant.id,
                                "quantity": grant.quantity,
                                "granted_at": grant.granted_at.isoformat(),
                                "expires_at": (
                                    grant.expires_at.isoformat()
                                    if grant.expires_at
                                    else None
                                ),
                            }
                            for grant in grants
                        ],
                    }
                
                base = get_effective_subscription_price(item)

                pricing_result = apply_discounts(
                    member=member,
                    member_adjustments=member_adjustments,
                    member_id=member.id,
                    base_amount=base,
                    discount_type="subscription",
                    plan=plan,
                    member_subscription_discounts=member_subscription_discounts,
                    member_joining_discounts=member_joining_discounts,
                    subscription_discount_plan_ids=subscription_discount_plan_ids,
                    joining_discount_plan_ids=joining_discount_plan_ids,
                )

                subscription_items.append({
                    "item_id": item.id,
                    "plan_id": plan.id,
                    "plan_name": plan.name,
                    "deleted_at": item.deleted_at.isoformat() if item.deleted_at else None,
                    "access_until": item.access_until.isoformat() if item.access_until else None,
                    **pricing_result,
                    "ticket": ticket_data,
                })

            member_data["subscription_items"] = subscription_items

            # -------------------------
            # PLAN ALTERNATIVES
            # -------------------------
            plan_alternatives = {}

            for plan in plans:
                full_pricing = apply_discounts(
                    member=member,
                    member_adjustments=member_adjustments,
                    member_id=member.id,
                    base_amount=plan.price,
                    discount_type="subscription",
                    plan=plan,
                    member_subscription_discounts=member_subscription_discounts,
                    member_joining_discounts=member_joining_discounts,
                    subscription_discount_plan_ids=subscription_discount_plan_ids,
                    joining_discount_plan_ids=joining_discount_plan_ids,
                )

                # proration

                ticket_proration = None

                if plan.plan_type == "ticket_plan":

                    ticket_proration = calculate_ticket_proration(
                        today=today,
                        anchor_day=anchor_day,
                        plan_price=plan.price,
                        ticket_quantity=plan.ticket_quantity,
                        mode=mode,
                    )
                
                    proration = ticket_proration["calendar_proration"]
                
                    # IMPORTANT:
                    # Discount proration follows the rounded ticket quantity.
                    ratio = ticket_proration["ticket_ratio"]
                
                    prorated_pricing = apply_discounts(
                        member=member,
                        member_adjustments=member_adjustments,
                        member_id=member.id,
                        base_amount=ticket_proration["base_amount"],
                        discount_type="subscription",
                        plan=plan,
                        proration_ratio=ratio,
                        member_subscription_discounts=member_subscription_discounts,
                        member_joining_discounts=member_joining_discounts,
                        subscription_discount_plan_ids=subscription_discount_plan_ids,
                        joining_discount_plan_ids=joining_discount_plan_ids,
                    )
                
                else:

                    if mode == "regular":
                        proration = calculate_regular_proration(
                            today, anchor_day, plan.price
                        )
                        ratio = (
                            proration["remaining_days"]
                            / proration["billing_period_days"]
                        )
                    else:
                        proration = calculate_monthly_proration(
                            today, plan.price
                        )
                        ratio = (
                            proration["remaining_days"]
                            / proration["days_in_month"]
                        )
    
                    prorated_pricing = apply_discounts(
                        member=member,
                        member_adjustments=member_adjustments,
                        member_id=member.id,
                        base_amount=proration["prorated_amount"],
                        discount_type="subscription",
                        plan=plan,
                        proration_ratio=ratio,
                        member_subscription_discounts=member_subscription_discounts,
                        member_joining_discounts=member_joining_discounts,
                        subscription_discount_plan_ids=subscription_discount_plan_ids,
                        joining_discount_plan_ids=joining_discount_plan_ids,
                    )

                is_monthly_past_anchor = (
                    plan.plan_type != "ticket_plan"
                    and mode == "monthly"
                    and anchor_day
                    and today.day > anchor_day
                )

                if is_monthly_past_anchor:
                    today_charge = {
                        "base": prorated_pricing["base"] + full_pricing["base"],
                        "final": prorated_pricing["final"] + full_pricing["final"],
                        "savings": prorated_pricing["savings"] + full_pricing["savings"],
                    }
                else:
                    today_charge = prorated_pricing

                ticket_preview = None

                if plan.plan_type == "ticket_plan":

                    initial_expires_at = calculate_ticket_expiration(
                        plan=plan,
                        granted_at=timezone.now(),
                    )

                    ticket_preview = {
                        "ticket_type_id": plan.ticket_type_id,
                        "ticket_type_name": (
                            plan.ticket_type.name
                            if plan.ticket_type
                            else None
                        ),
                
                        # Normal recurring entitlement.
                        "monthly_quantity": plan.ticket_quantity,
                
                        # Initial signup entitlement after calendar
                        # proration + rounding.
                        "initial_quantity": ticket_proration[
                            "ticket_quantity"
                        ],
                
                        "raw_initial_quantity": ticket_proration[
                            "raw_ticket_quantity"
                        ],
                
                        "calendar_ratio": ticket_proration[
                            "calendar_ratio"
                        ],
                
                        # This is the ratio used for money/discounts.
                        "ticket_ratio": ticket_proration[
                            "ticket_ratio"
                        ],
                
                        "expiration_mode": (
                            plan.ticket_expiration_mode
                        ),
                
                        "expiration_days": (
                            plan.ticket_expiration_days
                        ),
                
                        # For the first grant, which happens today.
                        "initial_grant_date": today.isoformat(),
                
                        "initial_grant_expires_at": (
                            initial_expires_at.isoformat()
                            if initial_expires_at
                            else None
                        ),
                    }

                plan_alternatives[plan.id] = {
                    "plan_name": plan.name,
                    "monthly": full_pricing,
                    "prorated": {
                        **prorated_pricing,
                        "proration": proration,
                        "ratio": ratio,
                    },
                    "today_charge": today_charge,
                    "plan_type": plan.plan_type,
                    "ticket": ticket_preview,
                }

            member_data["plan_alternatives"] = plan_alternatives

        # =========================================================
        # STAFF VIEW
        # =========================================================
        elif can_view_all:
            staff_items = []

            for item in items_by_member.get(member.id, []):
                plan = item.plan
                if not plan:
                    continue

                ticket_data = None

                if plan.plan_type == "ticket_plan":

                    grants = [
                        grant
                        for grant in ticket_grants_by_member.get(member.id, [])
                        if grant.ticket_type_id == plan.ticket_type_id
                    ]
                
                    ticket_data = {
                        "ticket_type_id": plan.ticket_type_id,
                        "ticket_type_name": (
                            plan.ticket_type.name
                            if plan.ticket_type
                            else None
                        ),
                        "monthly_quantity": plan.ticket_quantity,
                        "expiration_mode": plan.ticket_expiration_mode,
                        "expiration_days": plan.ticket_expiration_days,
                        "grants": [
                            {
                                "id": grant.id,
                                "quantity": grant.quantity,
                                "granted_at": grant.granted_at.isoformat(),
                                "expires_at": (
                                    grant.expires_at.isoformat()
                                    if grant.expires_at
                                    else None
                                ),
                            }
                            for grant in grants
                        ],
                    }

                base = get_effective_subscription_price(item)

                pricing_result = apply_discounts(
                    member=member,
                    member_adjustments=member_adjustments,
                    member_id=member.id,
                    base_amount=base,
                    discount_type="subscription",
                    plan=plan,
                    member_subscription_discounts=member_subscription_discounts,
                    member_joining_discounts=member_joining_discounts,
                    subscription_discount_plan_ids=subscription_discount_plan_ids,
                    joining_discount_plan_ids=joining_discount_plan_ids,
                )

                staff_items.append({
                    "item_id": item.id,
                    "plan_id": plan.id,
                    "plan_name": plan.name,
                    "deleted_at": item.deleted_at.isoformat() if item.deleted_at else None,
                    "access_until": item.access_until.isoformat() if item.access_until else None,
                    **pricing_result,
                    "ticket": ticket_data,
                })

            member_data["subscription_items"] = staff_items

        # =========================================================
        # PUBLIC VIEW
        # =========================================================
        else:
            member_data = {
                "has_subscription": member.id in items_by_member
            }

        pricing_map[member.id] = member_data

    return pricing_map


from collections import defaultdict
def get_level_participation(member):
    level_sums = defaultdict(int)
    for p in member.participations.all():
        if p.level_counts:
            for lvl, count in p.level_counts.items():
                level_sums[int(lvl)] += count
    return dict(level_sums)

VALID_SUBDOMAIN_RE = re.compile(r'^[a-z0-9-]+$', re.IGNORECASE)



def hash_uploaded_file(uploaded_file, chunk_size=8192):
    hasher = hashlib.sha256()
    for chunk in uploaded_file.chunks(chunk_size):
        hasher.update(chunk)

    uploaded_file.seek(0)  

    return hasher.hexdigest()

def slugify(value):
    if not value:
        return ""

    value = str(value).strip()

    value = unicodedata.normalize("NFKC", value)

    value = re.sub(r"[^\w\s\-ぁ-んァ-ン一-龯]", "", value)

    value = re.sub(r"[\s\-]+", "-", value)

    return value.strip("-").lower()


class InvoiceViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = PaymentHistoryInvoiceSerializer

    def get_queryset(self):
        user = self.request.user

        if not user.is_authenticated:
            return Invoice.objects.none()

        club_id = self.request.query_params.get("club")
        member_id = self.request.query_params.get("member")

        if not club_id or not member_id:
            return Invoice.objects.none()

        # Only the club owner can view payment history.
        club = Club.objects.filter(
            id=club_id,
            owner=user,
            is_deleted=False,
        ).first()

        if not club:
            return Invoice.objects.none()

        # Make sure the requested member belongs to this club.
        member = Member.objects.filter(
            id=member_id,
            club=club,
        ).first()

        if not member:
            return Invoice.objects.none()

        # The member's subscription is the billing/family account.
        subscription = Subscription.objects.filter(
            owner=member.owner,
            club=club,
        ).order_by("-id").first()

        if not subscription:
            return Invoice.objects.none()

        return (
            Invoice.objects
            .filter(
                club=club,
                subscription=subscription,
                status="paid",
            )
            .select_related(
                "subscription",
                "payer",
            )
            .prefetch_related(
                "items",
            )
            .order_by("-issued_at", "-id")
        )

class MemberPricingAdjustmentViewSet(viewsets.ModelViewSet):
    queryset = MemberPricingAdjustment.objects.all()
    serializer_class = MemberPricingAdjustmentSerializer

    def get_queryset(self):
        qs = (
            MemberPricingAdjustment.objects.all()
            .select_related("member", "club")
            .order_by("-created_at")
        )

        club_id = self.request.query_params.get("club")
        member_id = self.request.query_params.get("member")

        if club_id:
            qs = qs.filter(club_id=club_id)

        if member_id:
            qs = qs.filter(member_id=member_id)

        return qs

    # -------------------------
    # CREATE
    # -------------------------
    def perform_create(self, serializer):
        club_id = self.request.data.get("club")
        member_id = self.request.data.get("member")

        if not club_id:
            raise serializers.ValidationError({"club": "club is required"})
        if not member_id:
            raise serializers.ValidationError({"member": "member is required"})

        club = Club.objects.filter(id=club_id, is_deleted=False).first()
        if not club:
            raise serializers.ValidationError({"club": "クラブが見つかりません。"})

        # permission check (same pattern as your other viewsets)
        if club.owner_id != self.request.user.id:
            raise serializers.ValidationError({"detail": "この操作を行う権限がありません。"})

        member = Member.objects.filter(id=member_id, club=club).first()
        if not member:
            raise serializers.ValidationError({"member": "Member not found"})

        serializer.save(club=club, member=member)

    # -------------------------
    # UPDATE
    # -------------------------
    def perform_update(self, serializer):
        obj = self.get_object()

        if obj.club.owner_id != self.request.user.id:
            raise serializers.ValidationError({"detail": "この操作を行う権限がありません。"})

        # optional safety: prevent cross-club reassignment
        serializer.save(club=obj.club)

    # -------------------------
    # DELETE
    # -------------------------
    def perform_destroy(self, instance):
        if instance.club.owner_id != self.request.user.id:
            raise serializers.ValidationError({"detail": "この操作を行う権限がありません。"})

        instance.delete()


class DiscountViewSet(viewsets.ModelViewSet):
    serializer_class = DiscountSerializer
    queryset = Discount.objects.all()

    def get_queryset(self):
        queryset = Discount.objects.all().prefetch_related("conditions", "plans")

        club_id = self.request.query_params.get("club")
        if club_id:
            queryset = queryset.filter(club_id=club_id)
    
        return queryset

    def perform_create(self, serializer):
        club_id = self.request.data.get("club")

        club = Club.objects.filter(id=club_id).first()

        if not club:
            raise serializers.ValidationError({"club": "クラブが見つかりません。"})

        if club.owner != self.request.user:
            raise serializers.ValidationError({"detail": "この操作を行う権限がありません。"})

        serializer.save(club=club)

    def perform_update(self, serializer):
        discount = self.get_object()

        if discount.club.owner != self.request.user:
            raise serializers.ValidationError({"detail": "この操作を行う権限がありません。"})

        serializer.save()

    def perform_destroy(self, instance):
        if instance.club.owner != self.request.user:
            raise serializers.ValidationError({"detail": "この操作を行う権限がありません。"})

        instance.delete()




class MembershipPlanViewSet(viewsets.ModelViewSet):
    queryset = MembershipPlan.objects.all()
    serializer_class = MembershipPlanSerializer

    def get_serializer_context(self):
        context = super().get_serializer_context()

        subdomain = self.request.data.get("club_subdomain")
        club = Club.objects.filter(
            subdomain=subdomain,
            is_deleted=False
        ).first()

        context["club"] = club
        return context

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        result = self.perform_destroy(instance)

        if result and result.get("scheduled"):
            return Response(
                {
                    "detail": (
                        "プランの削除を予約しました。既存の契約者の"
                        "解約処理が完了すると自動的に削除されます。"
                    ),
                    "scheduled_for_deletion": True,
                },
                status=status.HTTP_202_ACCEPTED,
            )

        return Response(status=status.HTTP_204_NO_CONTENT)

    def perform_destroy(self, instance):
        if self.request.user != instance.club.owner:
            raise serializers.ValidationError(
                {"detail": "プランを削除できるのはオーナーのみです。"}
            )

        if would_break_any_bundle(instance):
            raise serializers.ValidationError(
                "このプランを削除するとセットプランの内訳が2つ未満になるため、削除できません。先にセットプランを変更してください。"
            )

        # ---------------------------------------------------------
        # Already deleted / already scheduled → nothing to do.
        # ---------------------------------------------------------
        if instance.is_deleted or instance.scheduled_for_deletion:
            raise serializers.ValidationError(
                {
                    "detail": (
                        "このプランはすでに削除済み、または削除予約済みです。"
                    )
                }
            )

        # ---------------------------------------------------------
        # STEP 1: Check ANY subscription history
        # ---------------------------------------------------------
        has_history = SubscriptionItem.objects.filter(
            plan=instance
        ).exists()

        has_active_items = SubscriptionItem.objects.filter(
            plan=instance,
            deleted_at__isnull=True
        ).exists()

        # ---------------------------------------------------------
        # CASE 1: ACTIVE ITEMS EXIST → schedule background cancellation.
        #
        # We do NOT cancel potentially hundreds of Stripe subscription
        # items synchronously inside the HTTP request. Instead we mark
        # the plan as scheduled for deletion (blocking any new
        # activation immediately) and hand the actual cancellation
        # work off to delete_membership_plan_task, which is safe to
        # retry and is also backed by reconcile_scheduled_plan_deletions.
        # ---------------------------------------------------------
        if has_active_items:

            instance.scheduled_for_deletion = True
            instance.save(
                update_fields=[
                    "scheduled_for_deletion",
                ]
            )

            transaction.on_commit(
                lambda: delete_membership_plan_task.delay(instance.id)
            )

            return {"scheduled": True}

        # ---------------------------------------------------------
        # STEP 3: HAS HISTORY, NO ACTIVE ITEMS → SOFT DELETE ONLY
        # ---------------------------------------------------------
        if has_history:

            instance.is_deleted = True
            instance.deleted_at = timezone.now()

            instance.save(
                update_fields=[
                    "is_deleted",
                    "deleted_at"
                ]
            )

            return None

        # ---------------------------------------------------------
        # STEP 4: NEVER USED → SAFE HARD DELETE
        # ---------------------------------------------------------
        instance.bundled_plans.clear()
        instance.delete()

        return None

    def perform_create(self, serializer):
        subdomain = self.request.data.get(
            "club_subdomain"
        )

        if not subdomain:
            raise serializers.ValidationError({
                "club_subdomain": (
                    "この項目は必須です。"
                )
            })

        club = Club.objects.filter(
            subdomain=subdomain,
            is_deleted=False
        ).first()

        if not club:
            raise serializers.ValidationError({
                "club_subdomain": "クラブが見つかりません。"
            })

        if self.request.user != club.owner:
            raise serializers.ValidationError({
                "detail": "プランを作成できるのはオーナーのみです。"
            })

        name = serializer.validated_data.get(
            "name"
        )

        price = serializer.validated_data.get(
            "price"
        )

        if not name:
            raise serializers.ValidationError({
                "name": "プラン名を入力してください。"
            })

        if price is None or price <= 0:
            raise serializers.ValidationError({
                "price": (
                    "料金は1円以上にしてください。"
                )
            })

        # ---------------------------------------------------------
        # Determine final plan type.
        #
        # 2+ bundled plans always means BUNDLE.
        # ---------------------------------------------------------

        bundled_plans = serializer.validated_data.get(
            "bundled_plans",
            []
        )

        plan_type = serializer.validated_data.get(
            "plan_type",
            MembershipPlan.PlanType.NORMAL
        )

        if len(bundled_plans) >= 2:
            plan_type = MembershipPlan.PlanType.BUNDLE

        # ---------------------------------------------------------
        # Ticket-plan validation
        # ---------------------------------------------------------

        if (
            plan_type
            == MembershipPlan.PlanType.TICKET_PLAN
        ):

            ticket_type = serializer.validated_data.get(
                "ticket_type"
            )

            ticket_quantity = serializer.validated_data.get(
                "ticket_quantity"
            )

            expiration_mode = serializer.validated_data.get(
                "ticket_expiration_mode",
                MembershipPlan.TicketExpirationMode.ONE_MONTH
            )

            expiration_days = serializer.validated_data.get(
                "ticket_expiration_days"
            )

            if not ticket_type:
                raise serializers.ValidationError({
                    "ticket_type": (
                        "チケットプランでは、発行するチケット種類を選択してください。"
                    )
                })

            if (
                ticket_quantity is None
                or ticket_quantity <= 0
            ):
                raise serializers.ValidationError({
                    "ticket_quantity": (
                        "毎月付与するチケット枚数は1以上にしてください。"
                    )
                })

            if (
                expiration_mode
                == MembershipPlan.TicketExpirationMode.DAYS_AFTER_GRANT
            ):

                if (
                    expiration_days is None
                    or expiration_days <= 0
                ):
                    raise serializers.ValidationError({
                        "ticket_expiration_days": (
                            "チケットの有効期限日数は1以上にしてください。"
                        )
                    })

        # ---------------------------------------------------------
        # Save plan first.
        # ---------------------------------------------------------

        plan = serializer.save(
            club=club,
            plan_type=plan_type,
        )

        # ---------------------------------------------------------
        # Create Stripe Product
        # ---------------------------------------------------------

        product_data = {
            "name": plan.name,
            "metadata": {
                "club_id": str(club.id),
                "plan_id": str(plan.id),
                "plan_type": plan.plan_type,
            },
        }

        if plan.description:
            product_data["description"] = (
                plan.description
            )

        product = stripe.Product.create(
            **product_data,
            stripe_account=club.stripe_account_id
        )

        # ---------------------------------------------------------
        # Create Stripe Price
        # ---------------------------------------------------------

        stripe_price = stripe.Price.create(
            product=product.id,
            unit_amount=int(plan.price),
            currency=plan.currency,
            recurring={
                "interval": plan.interval
            },
            stripe_account=club.stripe_account_id
        )

        # ---------------------------------------------------------
        # Save Stripe IDs
        # ---------------------------------------------------------

        plan.stripe_product_id = product.id
        plan.stripe_price_id = stripe_price.id

        plan.save(
            update_fields=[
                "stripe_product_id",
                "stripe_price_id",
            ]
        )

    def perform_update(self, serializer):
        plan = self.get_object()
        club = plan.club

        if self.request.user != club.owner:
            raise serializers.ValidationError({
                "detail": "プランを更新できるのはオーナーのみです。"
            })

        # ---------------------------------------------------------
        # Plan type is immutable.
        # ---------------------------------------------------------

        submitted_plan_type = (
            serializer.validated_data.get(
                "plan_type"
            )
        )

        if (
            submitted_plan_type is not None
            and submitted_plan_type != plan.plan_type
        ):
            raise serializers.ValidationError({
                "plan_type": (
                    "プランの種類は作成後に変更できません。"
                )
            })

        # Make absolutely sure it cannot be changed.
        serializer.validated_data.pop(
            "plan_type",
            None
        )

        old_price = plan.price
        old_name = plan.name
        old_description = plan.description

        updated_plan = serializer.save()

        stripe_account = club.stripe_account_id

        # ---------------------------------------------------------
        # Update Stripe Product
        # ---------------------------------------------------------

        if (
            updated_plan.stripe_product_id
            and (
                old_name != updated_plan.name
                or old_description
                != updated_plan.description
            )
        ):

            stripe.Product.modify(
                updated_plan.stripe_product_id,
                name=updated_plan.name,
                description=(
                    updated_plan.description or ""
                ),
                stripe_account=stripe_account
            )

        # ---------------------------------------------------------
        # Price changed → create NEW Stripe Price
        # ---------------------------------------------------------

        if old_price != updated_plan.price:

            new_price = stripe.Price.create(
                product=updated_plan.stripe_product_id,
                unit_amount=int(
                    updated_plan.price
                ),
                currency=updated_plan.currency,
                recurring={
                    "interval":
                        updated_plan.interval
                },
                stripe_account=stripe_account
            )

            updated_plan.stripe_price_id = (
                new_price.id
            )

            updated_plan.save(
                update_fields=[
                    "stripe_price_id"
                ]
            )


class SlateImageViewSet(viewsets.ModelViewSet):
    queryset = SlateImage.objects.all()
    serializer_class = SlateImageSerializer


    def perform_create(self, serializer):
        serializer.save(
            club_id=self.request.data.get("club")
        )

    def create(self, request, *args, **kwargs):
        club_id = request.data.get("club")
        image_file = request.FILES.get("image")

        if not club_id or not image_file:
            return Response(
                {"detail": "club and image are required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
 
        image_hash = hash_uploaded_file(image_file)
 
        existing = SlateImage.objects.filter(
            club_id=club_id,
            hash=image_hash,
        ).first()

        if existing:
            existing.created_at = timezone.now()
            existing.save(update_fields=["created_at"])
            
            serializer = self.get_serializer(existing)
            return Response(serializer.data, status=status.HTTP_200_OK)
 
        serializer = self.get_serializer(
            data={
                "image": image_file,
                "hash": image_hash,
            }
        )
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)

        return Response(
            serializer.data,
            status=status.HTTP_201_CREATED,
        )





class JoinRequestViewSet(viewsets.ModelViewSet):
    queryset = JoinRequest.objects.all()
    serializer_class = JoinRequestSerializer

    def perform_create(self, serializer):
        subdomain = self.request.data.get("club_subdomain")
        is_family = self.request.data.get("is_family") == "true"
    
        club = Club.objects.filter(
            subdomain=subdomain,
            is_deleted=False
        ).first()
    
        if not club:
            raise serializers.ValidationError(
                {"club_subdomain": "クラブが見つかりません。"}
            )
    
        if not is_family:
            # Only restrict normal users
            existing = JoinRequest.objects.filter(
                user=self.request.user,
                owner=self.request.user,
                club=club,
            ).first()
    
            if existing:
                raise serializers.ValidationError(
                    {"detail": "すでに入会申請を送信しています。承認をお待ちください。"}
                )

        selected_plans = serializer.validated_data.get(
            "already_subscribed_plans",
            []
        )
        
        selected_plan_ids = {
            plan.id
            for plan in selected_plans
        }
        
        # ---------------------------------------------------------
        # Validate selected legacy plans
        # ---------------------------------------------------------
        
        invalid_plans = [
            plan
            for plan in selected_plans
            if (
                plan.club_id != club.id
                or plan.is_deleted
                or plan.scheduled_for_deletion
                or not plan.active
            )
        ]
        
        if invalid_plans:
            raise serializers.ValidationError(
                {
                    "already_subscribed_plans": (
                        "選択したプランの一部が無効です。削除済みまたは利用できないプランが含まれています。"
                    )
                }
            )

        # ---------------------------------------------------------
        # Validate group/bundle rules across ALL selected plans
        # ---------------------------------------------------------
        
        try:
            validate_plan_set(
                plan_ids=selected_plan_ids,
                club=club,
            )
        except ValidationError as e:
            raise serializers.ValidationError(
                {
                    "already_subscribed_plans": format_validation_error(e)
                }
            )

            
        serializer.save(
            user=None if is_family else self.request.user,
            owner=self.request.user,
            club=club,
        )


    @action(detail=False, methods=["post"])
    def bulk_accept(self, request):
        ids = request.data.get("ids", [])
    
        if not isinstance(ids, list) or not ids:
            return Response(
                {"detail": "ids must be a non-empty list"},
                status=status.HTTP_400_BAD_REQUEST
            )
    
        join_requests = JoinRequest.objects.filter(id__in=ids).select_related("club")

        if join_requests.exclude(club__owner_id=request.user.id).exists():
            return Response({"detail": "この操作を行う権限がありません。"}, status=403)
    
        if not join_requests.exists():
            return Response(
                {"detail": "No join requests found"},
                status=status.HTTP_404_NOT_FOUND
            )

        if join_requests.values("club_id").distinct().count() > 1:
            return Response(
                {"detail": "Please accept join requests from one club at a time."},
                status=status.HTTP_400_BAD_REQUEST
            )
    
        created_member_ids = []

        club = join_requests.first().club
    
        with transaction.atomic():
            for jr in join_requests:
    
                new_picture = None
    
                if jr.picture:
                    jr.picture.open()
                    file_content = jr.picture.read()
                    jr.picture.close()
    
                    base_name = os.path.basename(jr.picture.name)
                    new_filename = f"{jr.club.subdomain}/members/{base_name}"
    
                    new_picture = ContentFile(file_content)
                    new_picture.name = new_filename
    
                member = Member.objects.create(
                    club=jr.club,
                    user=jr.user,
                    owner=jr.owner,
                    full_name=jr.full_name,
                    furigana=jr.furigana,
                    birth_date=jr.birth_date,
                    gender=jr.gender,
                    phone_number=jr.phone_number,
                    emergency_number=jr.emergency_number,
                    other_information=jr.other_information,
                    picture=new_picture,
                    level=jr.level or 1,
                )

                legacy_plans = list(
                    jr.already_subscribed_plans.all()
                )

                if legacy_plans:
                    LegacySubscriptionService.create_legacy_subscription(
                        club=jr.club,
                        member=member,
                        plans=legacy_plans,
                    )
    
                created_member_ids.append(member.id)
        
            # delete after processing
            join_requests.delete()
        
        sync_member_quantity(club)
    
        return Response(
            {
                "created_member_ids": created_member_ids
            },
            status=status.HTTP_201_CREATED
        )
            
            
    @action(detail=False, methods=["post"])
    def bulk_reject(self, request):
        ids = request.data.get("ids", [])

        if not isinstance(ids, list) or not ids:
            return Response(
                {"detail": "ids must be a non-empty list"},
                status=status.HTTP_400_BAD_REQUEST
            )
    
        join_requests = JoinRequest.objects.filter(id__in=ids).select_related("club")
    
        if not join_requests.exists():
            return Response(
                {"detail": "No join requests found"},
                status=status.HTTP_404_NOT_FOUND
            )
    
        # ❗ validate ownership BEFORE delete
        if join_requests.exclude(club__owner_id=request.user.id).exists():
            return Response(
                {"detail": "一部の申請に対してこの操作を行う権限がありません。"},
                status=status.HTTP_403_FORBIDDEN
            )
    
        join_requests.delete()

        return Response(
            {"deleted_ids": list(join_requests.values_list("id", flat=True))},
            status=status.HTTP_200_OK
        )




class MemberViewSet(viewsets.ModelViewSet):
    queryset = Member.objects.all()
    serializer_class = MemberSerializer


    @action(detail=True, methods=["delete"])
    def remove(self, request, pk=None):
        """Delete a member"""
        member = self.get_object()

        if member.club.owner_id != request.user.id:
            return Response(
                {"detail": "許可なし"},
                status=403
            )
        
        active_subscription = Subscription.objects.filter(
            owner=member.owner,
            club=member.club,
            status__in=[
                "active",
                "trialing",
                "past_due",
                "pending",
            ],
        ).exists()
    
        if active_subscription:
            return Response(
                {
                    "detail": "有効なサブスクリプションが存在するため、この会員を削除できません。"
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        club = member.club


        member.delete()
        sync_member_quantity(club)

        return Response({"status": "deleted"}, status=status.HTTP_200_OK)


class TicketTypeViewSet(viewsets.ModelViewSet):
    serializer_class = TicketTypeSerializer
    queryset = TicketType.objects.all()

    def get_serializer_context(self):
        context = super().get_serializer_context()

        if self.request.method in ["POST", "PUT", "PATCH"]:
            subdomain = self.request.data.get("club_subdomain")
        else:
            subdomain = self.request.query_params.get("club_subdomain")

        club = Club.objects.filter(
            subdomain=subdomain,
            is_deleted=False,
        ).first()

        context["club"] = club

        return context

    def get_queryset(self):
        qs = (
            TicketType.objects
            .select_related("club")
            .prefetch_related("eligible_plans")
            .order_by("name")
        )

        subdomain = (
            self.request.query_params.get("club_subdomain")
        )

        if subdomain:
            qs = qs.filter(
                club__subdomain=subdomain,
                club__is_deleted=False,
            )

        # Only show ticket types belonging to clubs
        # owned by the requesting user.
        if self.request.user.is_authenticated:
            qs = qs.filter(
                club__owner=self.request.user
            )
        else:
            qs = qs.none()

        return qs

    def perform_create(self, serializer):
        subdomain = self.request.data.get("club_subdomain")

        if not subdomain:
            raise serializers.ValidationError({
                "club_subdomain": "この項目は必須です。"
            })

        club = Club.objects.filter(
            subdomain=subdomain,
            is_deleted=False,
        ).first()

        if not club:
            raise serializers.ValidationError({
                "club_subdomain": "クラブが見つかりません。"
            })

        if club.owner_id != self.request.user.id:
            raise serializers.ValidationError({
                "detail": "チケット種類を作成できるのはオーナーのみです。"
            })

        serializer.save(club=club)

    def perform_update(self, serializer):
        ticket_type = self.get_object()

        if ticket_type.club.owner_id != self.request.user.id:
            raise serializers.ValidationError({
                "detail": "チケット種類を更新できるのはオーナーのみです。"
            })

        serializer.save(club=ticket_type.club)

    def perform_destroy(self, instance):
        if instance.club.owner_id != self.request.user.id:
            raise serializers.ValidationError({
                "detail": "チケット種類を削除できるのはオーナーのみです。"
            })

        # Don't actually delete it if packages/grants may reference it.
        instance.active = False
        instance.save(update_fields=["active"])

class TicketPackageViewSet(viewsets.ModelViewSet):
    serializer_class = TicketPackageSerializer
    queryset = TicketPackage.objects.all()

    def get_serializer_context(self):
        context = super().get_serializer_context()

        if self.request.method in ["POST", "PUT", "PATCH"]:
            subdomain = self.request.data.get("club_subdomain")
        else:
            subdomain = self.request.query_params.get("club_subdomain")

        club = Club.objects.filter(
            subdomain=subdomain,
            is_deleted=False,
        ).first()

        context["club"] = club

        return context

    def get_queryset(self):
        qs = (
            TicketPackage.objects
            .select_related(
                "club",
                "ticket_type",
            )
            .order_by("price", "name")
        )

        subdomain = self.request.query_params.get(
            "club_subdomain"
        )

        if subdomain:
            qs = qs.filter(
                club__subdomain=subdomain,
                club__is_deleted=False,
            )

        if self.request.user.is_authenticated:
            qs = qs.filter(
                club__owner=self.request.user
            )
        else:
            qs = qs.none()

        return qs

    def perform_create(self, serializer):
        subdomain = self.request.data.get("club_subdomain")

        if not subdomain:
            raise serializers.ValidationError({
                "club_subdomain": "この項目は必須です。"
            })

        club = Club.objects.filter(
            subdomain=subdomain,
            is_deleted=False,
        ).first()

        if not club:
            raise serializers.ValidationError({
                "club_subdomain": "クラブが見つかりません。"
            })

        if club.owner_id != self.request.user.id:
            raise serializers.ValidationError({
                "detail": "チケットパッケージを作成できるのはオーナーのみです。"
            })

        if not club.stripe_account_id:
            raise serializers.ValidationError({
                "detail": (
                    "このクラブではオンライン決済が設定されていません。"
                )
            })

        package = serializer.save(
            club=club,
            currency="jpy",
        )

        try:
            product_data = {
                "name": package.name,
                "metadata": {
                    "club_id": str(club.id),
                    "ticket_package_id": str(package.id),
                    "ticket_type_id": str(package.ticket_type_id),
                },
            }

            if package.description:
                product_data["description"] = package.description

            product = stripe.Product.create(
                **product_data,
                stripe_account=club.stripe_account_id,
            )

            stripe_price = stripe.Price.create(
                product=product.id,
                unit_amount=int(package.price),
                currency=package.currency,
                stripe_account=club.stripe_account_id,
            )

            package.stripe_price_id = stripe_price.id
            package.stripe_product_id = product.id

            package.save(
                update_fields=[
                    "stripe_price_id",
                    "stripe_product_id"
                ]
            )

        except Exception:
            package.delete()
            raise

    def perform_update(self, serializer):
        package = self.get_object()

        if package.club.owner_id != self.request.user.id:
            raise serializers.ValidationError({
                "detail": "チケットパッケージを更新できるのはオーナーのみです。"
            })

        old_name = package.name
        old_description = package.description
        old_price = package.price
        old_currency = package.currency
        old_stripe_price_id = package.stripe_price_id

        updated_package = serializer.save(
            club=package.club,
        )

        club = package.club

        if not club.stripe_account_id:
            raise serializers.ValidationError({
                "detail": (
                    "このクラブではオンライン決済が設定されていません。"
                )
            })

        stripe_account = club.stripe_account_id

        # -----------------------------------------
        # Update Stripe Product
        # -----------------------------------------

        # We need the product ID. Since your current
        # TicketPackage only stores stripe_price_id,
        # we should retrieve the Price to find the product.
        # -----------------------------------------

        if old_stripe_price_id:
            stripe_price = stripe.Price.retrieve(
                old_stripe_price_id,
                stripe_account=stripe_account,
            )

            product_id = stripe_price.product

            if (
                old_name != updated_package.name
                or old_description != updated_package.description
            ):
                stripe.Product.modify(
                    product_id,
                    name=updated_package.name,
                    description=updated_package.description or "",
                    stripe_account=stripe_account,
                )

            # -----------------------------------------
            # Price changed → create a NEW Stripe Price
            # -----------------------------------------

            if (
                old_price != updated_package.price
                or old_currency != updated_package.currency
            ):
                new_price = stripe.Price.create(
                    product=product_id,
                    unit_amount=int(updated_package.price),
                    currency=updated_package.currency,
                    stripe_account=stripe_account,
                )

                # Disable the old price.
                stripe.Price.modify(
                    old_stripe_price_id,
                    active=False,
                    stripe_account=stripe_account,
                )

                updated_package.stripe_price_id = new_price.id

                updated_package.save(
                    update_fields=[
                        "stripe_price_id",
                    ]
                )

    def perform_destroy(self, instance):
        if instance.club.owner_id != self.request.user.id:
            raise serializers.ValidationError({
                "detail": "チケットパッケージを削除できるのはオーナーのみです。"
            })
    
        instance.active = False
        instance.save(update_fields=["active"])
    
        if instance.stripe_price_id and instance.club.stripe_account_id:
            try:
                stripe.Price.modify(
                    instance.stripe_price_id,
                    active=False,
                    stripe_account=instance.club.stripe_account_id,
                )
            except stripe.error.StripeError:
                # Don't destroy the local package just because
                # Stripe archival failed.
                raise serializers.ValidationError({
                    "detail": (
                        "Stripe上の価格を無効化できなかったため、"
                        "パッケージを無効化できませんでした。"
                    )
                })
    
class ReservationViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = ReservationSerializer

    # ---------------------------------------------------------
    # Shared queryset optimization
    # ---------------------------------------------------------

    def get_base_queryset(self):
        return (
            Reservation.objects
            .select_related(
                "club",
                "lesson",
                "lesson__instructor",
                "lesson__instructor__user",
                "member",
                "user",
            )
            .filter(
                status=Reservation.Status.PAID,
            )
            .order_by(
                "reservation_date",
                "lesson__start_time",
                "id",
            )
        )

    # ---------------------------------------------------------
    # Default queryset
    #
    # We don't expose a generic unfiltered reservation list.
    # ---------------------------------------------------------

    def get_queryset(self):
        return Reservation.objects.none()

    # =========================================================
    # MY RESERVATIONS
    #
    # Works for:
    # - regular members
    # - logged-in non-members
    #
    # Both are identified by Reservation.user.
    #
    # GET:
    # /api/reservations/my/<club_id>/
    # =========================================================

    @action(
        detail=False,
        methods=["get"],
        url_path=r"my/(?P<club_id>\d+)",
    )
    def my_reservations(
        self,
        request,
        club_id=None,
    ):
        if not request.user.is_authenticated:
            return Response(
                {
                    "detail":
                        "ログインが必要です。"
                },
                status=status.HTTP_401_UNAUTHORIZED,
            )

        club = (
            Club.objects
            .filter(
                id=club_id,
                is_deleted=False,
            )
            .first()
        )

        if not club:
            return Response(
                {
                    "detail":
                        "クラブが見つかりません。"
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        owned_member_ids = list(
            Member.objects
            .filter(club=club, owner=request.user)
            .values_list("id", flat=True)
        )
        qs = (
            self.get_base_queryset()
            .filter(
                club=club,
                reservation_date__gte=timezone.localdate(),
            )
        )
        if owned_member_ids:
            qs = qs.filter(member_id__in=owned_member_ids)
        else:
            email = (request.user.email or "").strip()
            if not email:
                qs = qs.none()
            else:
                qs = qs.filter(email__iexact=email)

        serializer = self.get_serializer(
            qs,
            many=True,
        )

        return Response(
            serializer.data,
            status=status.HTTP_200_OK,
        )

    # =========================================================
    # CLUB RESERVATIONS
    #
    # Owner only.
    #
    # Includes recent history + future reservations.
    #
    # GET:
    # /api/reservations/club/<club_id>/
    # =========================================================

    @action(
        detail=False,
        methods=["get"],
        url_path=r"club/(?P<club_id>\d+)",
    )
    def club_reservations(
        self,
        request,
        club_id=None,
    ):
        if not request.user.is_authenticated:
            return Response(
                {
                    "detail":
                        "ログインが必要です。"
                },
                status=status.HTTP_401_UNAUTHORIZED,
            )

        club = (
            Club.objects
            .filter(
                id=club_id,
                owner=request.user,
                is_deleted=False,
            )
            .first()
        )

        if not club:
            return Response(
                {
                    "detail":
                        "このクラブの予約情報を"
                        "閲覧する権限がありません。"
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        qs = (
            self.get_base_queryset()
            .filter(
                club=club,
                reservation_date__gte=timezone.localdate(),
            )
        )

        serializer = self.get_serializer(
            qs,
            many=True,
        )

        return Response(
            serializer.data,
            status=status.HTTP_200_OK,
        )

    # =========================================================
    # INSTRUCTOR RESERVATIONS
    #
    # Only reservations for lessons taught by the requesting
    # instructor.
    #
    # Instructor check:
    #
    # request.user == reservation.lesson.instructor.user
    #
    # GET:
    # /api/reservations/instructor/<club_id>/
    # =========================================================

    @action(
        detail=False,
        methods=["get"],
        url_path=r"instructor/(?P<club_id>\d+)",
    )
    def instructor_reservations(
        self,
        request,
        club_id=None,
    ):
        if not request.user.is_authenticated:
            return Response(
                {
                    "detail":
                        "ログインが必要です。"
                },
                status=status.HTTP_401_UNAUTHORIZED,
            )

        club = (
            Club.objects
            .filter(
                id=club_id,
                is_deleted=False,
            )
            .first()
        )

        if not club:
            return Response(
                {
                    "detail":
                        "クラブが見つかりません。"
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        # -----------------------------------------------------
        # Make sure the requesting user is actually an
        # instructor belonging to this club.
        #
        # We use the same relationship you described:
        #
        # request.user == reservation.lesson.instructor.user
        # -----------------------------------------------------

        instructor_member = (
            Member.objects
            .filter(
                club=club,
                is_instructor=True,
            )
            .filter(
                Q(user=request.user) | Q(owner=request.user)
            )
            .first()
        )

        if not instructor_member:
            return Response(
                {
                    "detail":
                        "この予約情報を閲覧する権限がありません。"
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        qs = (
            self.get_base_queryset()
            .filter(
                club=club,
                reservation_date__gte=timezone.localdate(),
            )
        )

        serializer = self.get_serializer(
            qs,
            many=True,
        )

        return Response(
            serializer.data,
            status=status.HTTP_200_OK,
        )

class LessonViewSet(viewsets.ModelViewSet):
    queryset = Lesson.objects.all()
    serializer_class = LessonSerializer

    def get_serializer_context(self):
        context = super().get_serializer_context()

        subdomain = self.request.data.get("club_subdomain")

        # NEW: Find the club from the submitted subdomain.
        club = Club.objects.filter(
            subdomain=subdomain,
            is_deleted=False
        ).first()

        # NEW: Pass the club into LessonSerializer.
        context["club"] = club

        return context

    def perform_create(self, serializer):
        subdomain = self.request.data.get("club_subdomain")
        if not subdomain:
            raise serializers.ValidationError({"club_subdomain": "この項目は必須です。"})

        club = Club.objects.filter(subdomain=subdomain, is_deleted=False).first()
        if not club:
            raise serializers.ValidationError({"club_subdomain": "クラブが見つかりません。"})
        
        section_id = self.request.data.get("section_id")

        if section_id is None:
            raise serializers.ValidationError(
                {"section_id": "この項目は必須です。"}
            )
        instructor = None
        instructor_id = self.request.data.get("instructor_id")
        if instructor_id:
            try:
                instructor_id = int(instructor_id)
                instructor = Member.objects.filter(id=instructor_id, is_instructor=True, club=club).first()
            except ValueError:
                raise serializers.ValidationError({"instructor_id": "Invalid ID."})

            if not instructor:
                raise serializers.ValidationError({"instructor_id": "Instructor not found or not valid."})

        serializer.save(club=club, instructor=instructor, section_id=section_id)



class ParticipationViewSet(viewsets.ModelViewSet):
    queryset = Participation.objects.all()
    serializer_class = ParticipationSerializer

    def get_member_stats(self, member):
        participations = member.participations.all()

        total = (
            member.manual_total_participation
            + sum(p.total_count for p in participations)
        )

        monthly = sum(
            p.monthly_count for p in participations
        )

        return {
            "total_participation": total,
            "this_month_participation": monthly,
            "level_participation": get_level_participation(member),
        }

    @action(detail=False, methods=["post"], url_path="bulk-create")
    def bulk_create(self, request):
        with transaction.atomic():

            lesson_id = request.data.get("lesson")
            member_ids = request.data.get("members", [])

            if not lesson_id or not member_ids:
                return Response(
                    {"detail": "lessonとmembersは必須です"},
                    status=status.HTTP_400_BAD_REQUEST
                )

            lesson = Lesson.objects.filter(id=lesson_id).first()

            if not lesson:
                return Response(
                    {"detail": "レッスンが存在しません"},
                    status=status.HTTP_404_NOT_FOUND
                )

            # permission
            if lesson.club.owner_id != request.user.id:
                return Response(
                    {"detail": "許可されていません"},
                    status=status.HTTP_403_FORBIDDEN
                )

            members = Member.objects.filter(
                id__in=member_ids,
                club=lesson.club,
            )

            if not members.exists():
                return Response(
                    {"detail": "メンバーが存在しません"},
                    status=status.HTTP_404_NOT_FOUND
                )

            today = timezone.localtime(timezone.now()).date()

            try:
                milestones = lesson.club.level_milestones or {}

                if isinstance(milestones, str):
                    milestones = json.loads(milestones)

            except Exception:
                milestones = {}


            leveled_up_members = []
            created_members = []

            updated_members = []

            for member in members:

                participation, created = Participation.objects.get_or_create(
                    member=member,
                    lesson=lesson,
                    defaults={
                        "total_count": 0,
                        "monthly_count": 0,
                        "level_counts": {},
                    }
                )


                # already marked today
                if participation.last_participation_date == today:
                    stats = self.get_member_stats(member)

                    updated_members.append({
                        "id": member.id,
                        **stats,
                        "level": member.level,
                    })

                    continue


                if participation.level_counts is None:
                    participation.level_counts = {}


                current_level = member.level
                level_key = str(current_level)


                participation.total_count += 1
                participation.monthly_count += 1

                participation.level_counts[level_key] = (
                    participation.level_counts.get(level_key, 0) + 1
                )


                participation.last_participation_date = today

                participation.save()


                created_members.append(member.id)


                # -------------------------
                # LEVEL CHECK
                # -------------------------

                level_totals = get_level_participation(member)

                total_for_current_level = level_totals.get(
                    member.level,
                    0
                )

                required = milestones.get(
                    str(member.level)
                )


                if required and total_for_current_level >= required:

                    member.level += 1
                    member.save()

                    leveled_up_members.append({
                        "member_id": member.id,
                        "new_level": member.level,
                    })
                
                stats = self.get_member_stats(member)

                updated_members.append({
                    "id": member.id,
                    **stats,
                    "level": member.level,
                })


            return Response(
                {
                    "created": created_members,
                    "level_ups": leveled_up_members,
                    "updated_members": updated_members,
                },
                status=status.HTTP_201_CREATED
            )


class ClubViewSet(viewsets.ModelViewSet):
    serializer_class = ClubSerializer
    queryset = Club.objects.all()

    def get_queryset(self):
        return (
            Club.objects.filter(is_deleted=False)
            .prefetch_related(

                Prefetch(
                    "members",
                    queryset=Member.objects.prefetch_related(
                        "participations",
                        Prefetch(
                           "subscription_items",
                            queryset=SubscriptionItem.objects
                            .select_related("subscription", "plan", "source_item")
                            .prefetch_related("replacement_for").filter(
                                Q(deleted_at__isnull=True) |
                                Q(deleted_at__isnull=False, access_until__gt=now()),
                                subscription__status__in=["active", "trialing", "pending"],
                            ),
                        ),
                        Prefetch(
                            "ticket_grants",
                            queryset=(
                                TicketGrant.objects
                                .select_related("ticket_type")
                                .prefetch_related(
                                    "ticket_type__eligible_plans",
                                    "usages",
                                )
                            ),
                        ),
                    ),
                ),

                Prefetch(
                    "lessons",
                    queryset=Lesson.objects.prefetch_related("participations"),
                ),
                "slate_images",
                "join_requests",
                "membership_plans",
            )
        )

    @action(detail=False, methods=["get"], url_path="by-subdomain/(?P<subdomain>[^/.]+)")
    def by_subdomain(self, request, subdomain=None):
        club = self.get_queryset().filter(subdomain=subdomain).first()
        if not club:
            return Response({"detail": "Not found"}, status=status.HTTP_404_NOT_FOUND)

        serializer = self.get_serializer(club, context={"request": request})
        return Response(serializer.data)

    @action(detail=True, methods=["post"], url_path="pricing_preview")
    def pricing_preview(self, request, pk=None):
    
        club = self.get_object()
    
        preview_member = PreviewMember(
            gender=request.data.get("gender"),
            age=request.data.get("age"),
            family_count=request.data.get("family_count", 0),
        )
    
        data = build_pricing_map(
            club,
            request.user if request.user.is_authenticated else None,
            preview_member=preview_member,
        )

        return Response(data[-1])

    @action(detail=True, methods=["get"])
    def pricing_map(self, request, pk=None):
   
        club = self.get_object()
        data = build_pricing_map(club, request.user)

        return Response(data)


    @action(detail=False, methods=["post"], url_path="create-trial")
    def create_trial(self, request):
        subdomain = request.data.get("subdomain")

        
        if not subdomain:
            return Response({"error": "サブドメインは必須項目です。"}, status=status.HTTP_400_BAD_REQUEST)

        if not VALID_SUBDOMAIN_RE.match(subdomain):
            return Response(
                {"error": "サブドメインは英数字とハイフンのみ使用可能です。"},
                status=status.HTTP_400_BAD_REQUEST
            )

        if subdomain.startswith("-") or subdomain.endswith("-"):
            return Response(
                {"error": "サブドメインはハイフンで始めたり終えたりできません。"},
                status=status.HTTP_400_BAD_REQUEST
            )

        if Club.objects.filter(subdomain=subdomain, is_deleted=False).exists():
            return Response(
                {"error": "このサブドメインはすでに使用されています。"},
                status=status.HTTP_400_BAD_REQUEST
            )

        FORBIDDEN_SUBDOMAINS = ["www", "kaibaru"]

        if subdomain.lower() in FORBIDDEN_SUBDOMAINS:
            return Response(
                {"error": f"サブドメイン '{subdomain}' は使用できません。"},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        club = Club.objects.create(subdomain=subdomain, owner=request.user, expiration_date = timezone.now())

        serializer = self.get_serializer(club, context={"request": request})
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["patch"], url_path="update_section_layout")
    def update_section_layout(self, request, pk=None):
        club = self.get_object()

        section_id = request.data.get("section_id")
        layout = request.data.get("layout")
    
        if not section_id or not isinstance(layout, dict):
            return Response(
                {"detail": "section_id and layout are required"},
                status=status.HTTP_400_BAD_REQUEST
            )
    
        try:
            page_content = json.loads(club.page_content)
        except Exception:
            return Response(
                {"detail": "Invalid page_content"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
    
        section = page_content.get("sections", {}).get(str(section_id))
    
        if not section:
            return Response(
                {"detail": "Section not found"},
                status=status.HTTP_404_NOT_FOUND
            )
    
        if section.get("type") != "custom":
            return Response(
                {"detail": "Only custom sections can have layouts"},
                status=status.HTTP_400_BAD_REQUEST
            )
    
        section["layout"] = layout
    
        club.page_content = json.dumps(page_content)
        club.save()
    
        serializer = self.get_serializer(club, context={"request": request})
        return Response(serializer.data, status=status.HTTP_200_OK)
    
    

    @action(detail=True, methods=["post"], url_path="mutate_page_section")
    def mutate_page_section(self, request, pk=None):
      with transaction.atomic():
        club = self.get_object()

        action_type = request.data.get("action")
        section_id = request.data.get("section")  # only for remove
        title = request.data.get("title", "")
        style = request.data.get("style")
        requested_order = request.data.get("order")
        section_type = request.data.get("type", "custom")
        icon = request.data.get("icon")

        ALLOWED_TYPES = {"custom", "schedule", "join", "member", "teacher", "slideshow", "header", "memberplans", "event"}

        if action_type not in {"add", "remove", "edit", "add_slide", "remove_slide", "update_slide"}:
            return Response(
                {"detail": "Invalid action"},
                status=status.HTTP_400_BAD_REQUEST
            )

        if action_type == "add" and section_type not in ALLOWED_TYPES:
            return Response(
                {"detail": "Invalid section type"},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            page_content = json.loads(club.page_content) if club.page_content else {
                "version": 1,
                "next_section_id": 1,
                "sections": {}
            }
        except json.JSONDecodeError:
            return Response(
                {"detail": "Invalid page_content JSON"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        sections = page_content.setdefault("sections", {})
        next_id = page_content.setdefault("next_section_id", 1)

        # Convert to list for ordering logic
        section_list = list(sections.values())

        # ----- RULE ENFORCEMENT -----

        has_join = any(
            s.get("type") == "join"
            for s in section_list
        )

        if action_type == "add" and section_type in {"member", "teacher"} and not has_join:
            return Response(
                {"detail": "Join section is required before adding this section"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Prevent duplicate system sections

        UNIQUE_TYPES = {"join", "member", "teacher", "header", "memberplans"}
        if action_type == "add" and section_type in UNIQUE_TYPES:
            if any(s.get("type") == section_type for s in section_list):
                return Response(
                    {"detail": "This section type already exists"},
                    status=status.HTTP_400_BAD_REQUEST
                )

        # ----- ADD SECTION -----
        if action_type == "add":
            total_sections = len(section_list)

            try:
                order = int(requested_order)
            except (TypeError, ValueError):
                order = total_sections + 1

            order = max(1, min(order, total_sections + 1))

            # Shift existing sections down
            for section in section_list:
                if section["order"] >= order:
                    section["order"] += 1

            new_id = next_id
            page_content["next_section_id"] = new_id + 1

            base_slug = slugify(title or f"section-{new_id}")

            existing_slugs = {s.get("slug") for s in section_list if s.get("slug")}
            slug = base_slug
            i = 2
            while slug in existing_slugs:
                slug = f"{base_slug}-{i}"
                i += 1

            new_section = {
                "id": new_id,
                "order": order,
                "title": title or f"Section {new_id}",
                "type": section_type,
                "slug": slug,
            }
            if style:
                new_section["style"] = style
            if icon:
                new_section["icon"] = icon

            # Only custom sections get editable content
            if section_type == "custom":
                new_section["layout"] = {
                    "version": 2,
                    "boxes": []
                }

            if section_type == "header":
                header_data = request.data.get("header", {})

                new_section["header"] = {
                    "logo": header_data.get("logo"),
                    "logoHeight": header_data.get("logoHeight", 40),
                    "title": header_data.get("title", ""),
                    "subtitle": header_data.get("subtitle", ""),
                    "transparent": header_data.get("transparent", False),
                    "collapseAllNav": header_data.get("collapseAllNav", False),
                }

            if section_type == "slideshow":
                slides = request.data.get("slides")

                if isinstance(slides, list) and len(slides) >= 1:
                    new_section["slides"] = slides
                else:
                    new_section["slides"] = [
                        {
                            "id": str(uuid.uuid4()),
                            "image": None,
                            "heading": "New Slide",
                            "subheading": "",
                            "buttonText": "",
                            "buttonLink": "",
                            "buttonSectionId": None,
                            "buttonBoxId": None,
                            "textAlign": "center"
                        }
                    ]

            if section_type == "event":
                try:
                    save_section_event(
                        club=club,
                        section_id=new_id,
                        title=title,
                        payload=request.data.get("event") or {},
                    )
                except ValueError as exc:
                    transaction.set_rollback(True)
                    return Response(
                        {"detail": str(exc)},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
            
            section_list.append(new_section)

        


        elif action_type == "edit":
            if not section_id or str(section_id) not in sections:
                return Response(
                    {"detail": "Section does not exist"},
                    status=status.HTTP_400_BAD_REQUEST
                )
        
            section = sections[str(section_id)]
            old_order = section["order"]

            if title and title != section.get("title"):
                old_slug = section.get("slug")
            
                # Build a set of all used slugs (current + previous) except this section
                existing_slugs = set()
                for s in section_list:
                    if s["id"] == section["id"]:
                        continue
                    if "slug" in s and s["slug"]:
                        existing_slugs.add(s["slug"])
                    if "previous_slugs" in s:
                        existing_slugs.update(s["previous_slugs"])
            
                # Generate a unique slug
                base_slug = slugify(title)
                slug = base_slug
                i = 2
                while slug in existing_slugs:
                    slug = f"{base_slug}-{i}"
                    i += 1
            
                # Save old slug in previous_slugs
                previous_slugs = section.setdefault("previous_slugs", [])
                if old_slug and old_slug != slug and old_slug not in previous_slugs:
                    previous_slugs.append(old_slug)
            
                section["slug"] = slug        

            # ---- title update ----
            if title:
                section["title"] = title

            if section.get("type") == "event" and "event" in request.data:
                try:
                    save_section_event(
                        club=club,
                        section_id=section["id"],
                        title=section.get("title") or title,
                        payload=request.data.get("event") or {},
                    )
                except ValueError as exc:
                    transaction.set_rollback(True)
                    return Response(
                        {"detail": str(exc)},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
            
            if style is not None:
                section["style"] = style
            
            if "icon" in request.data:
                section["icon"] = icon

            if section.get("type") == "header" and "header" in request.data:
                header_data = request.data.get("header")

                if isinstance(header_data, dict):
                    section["header"] = {
                        "logo": header_data.get("logo"),
                        "logoHeight": header_data.get("logoHeight", 40),
                        "title": header_data.get("title", ""),
                        "subtitle": header_data.get("subtitle", ""),
                        "transparent": header_data.get("transparent", False),
                        "collapseAllNav": header_data.get("collapseAllNav", False),
                    }

            # ---- slideshow slides update ----
            if section.get("type") == "slideshow" and "slides" in request.data:
                slides = request.data.get("slides")
            
                if isinstance(slides, list) and len(slides) >= 1:
                    section["slides"] = slides
                else:
                    return Response(
                        {"detail": "Slideshow must contain at least one slide"},
                        status=status.HTTP_400_BAD_REQUEST
                    )
                    
             # ---- order update ----
            try:
                new_order = int(requested_order)
            except (TypeError, ValueError):
                new_order = old_order
                    
            total_sections = len(section_list)
            new_order = max(1, min(new_order, total_sections))
        
            if new_order != old_order:
                for s in section_list:
                    if s["id"] == section["id"]:
                        continue
        
                    if new_order > old_order:
                        # moving down
                        if old_order < s["order"] <= new_order:
                            s["order"] -= 1
                    else:
                        # moving up
                        if new_order <= s["order"] < old_order:
                            s["order"] += 1
        
                section["order"] = new_order
        
        
        # ----- ADD SLIDE -----
        elif action_type == "add_slide":
            if not section_id or str(section_id) not in sections:
                return Response(
                    {"detail": "Section does not exist"},
                    status=status.HTTP_400_BAD_REQUEST
                )

            section = sections[str(section_id)]

            if section.get("type") != "slideshow":
                return Response(
                    {"detail": "Not a slideshow section"},
                    status=status.HTTP_400_BAD_REQUEST
                )

            slides = section.setdefault("slides", [])

            slides.append({
                "id": str(uuid.uuid4()),
                "image": None,
                "heading": "New Slide",
                "subheading": "",
                "buttonText": "",
                "buttonLink": "",
                "textAlign": "center"
            })


        # ----- REMOVE SLIDE -----
        elif action_type == "remove_slide":
            slide_id = request.data.get("slide_id")

            if not section_id or str(section_id) not in sections:
                return Response(
                    {"detail": "Section does not exist"},
                    status=status.HTTP_400_BAD_REQUEST
                )

            section = sections[str(section_id)]

            if section.get("type") != "slideshow":
                return Response(
                    {"detail": "Not a slideshow section"},
                    status=status.HTTP_400_BAD_REQUEST
                )

            slides = section.get("slides", [])

            if len(slides) <= 1:
                return Response(
                    {"detail": "Slideshow must have at least one slide"},
                    status=status.HTTP_400_BAD_REQUEST
                )

            section["slides"] = [
                s for s in slides if str(s.get("id")) != str(slide_id)
            ]


        # ----- UPDATE SLIDE -----
        elif action_type == "update_slide":
            slide_id = request.data.get("slide_id")
            field = request.data.get("field")
            value = request.data.get("value")

            if not section_id or str(section_id) not in sections:
                return Response(
                    {"detail": "Section does not exist"},
                    status=status.HTTP_400_BAD_REQUEST
                )

            section = sections[str(section_id)]

            if section.get("type") != "slideshow":
                return Response(
                    {"detail": "Not a slideshow section"},
                    status=status.HTTP_400_BAD_REQUEST
                )

            valid_fields = {
                "heading",
                "subheading",
                "image",
                "buttonText",
                "buttonLink",
                "buttonSectionId",   # ADD
                "buttonBoxId",
                "textAlign",
            }

            if field not in valid_fields:
                return Response(
                    {"detail": "Invalid slide field"},
                    status=status.HTTP_400_BAD_REQUEST
                )

            for slide in section.get("slides", []):
                if str(slide.get("id")) == str(slide_id):
                    slide[field] = value

                    # ---- VALIDATION RULES ----

                    section_id_value = slide.get("buttonSectionId")
                    box_id_value = slide.get("buttonBoxId")
            
                    # Normalize empty values to None
                    if not section_id_value:
                        slide["buttonSectionId"] = None
                        slide["buttonBoxId"] = None
            
                    if slide.get("buttonBoxId") and not slide.get("buttonSectionId"):
                        return Response(
                            {"detail": "Box cannot be set without section"},
                            status=status.HTTP_400_BAD_REQUEST
                        )
                    break



        # ----- REMOVE SECTION -----
        elif action_type == "remove":
            if not section_id or str(section_id) not in sections:
                return Response(
                    {"detail": "Section does not exist"},
                    status=status.HTTP_400_BAD_REQUEST
                )

            removed_section = sections[str(section_id)]
            removed_order = removed_section["order"]

            if removed_section.get("type") == "event":
                Event.objects.filter(
                    club=club,
                    section_id=removed_section["id"],
                ).delete()

            # Prevent removing join if dependent sections exist
            if removed_section.get("type") == "join":
                if any(
                    s.get("type") in {"member", "teacher"}
                    for s in section_list
                ):
                    return Response(
                        {"detail": "Cannot remove join section while member or teacher sections exist"},
                        status=status.HTTP_400_BAD_REQUEST
                    )

            # Remove section
            section_list = [
                s for s in section_list if s["id"] != int(section_id)
            ]

            # Close ordering gap
            for section in section_list:
                if section["order"] > removed_order:
                    section["order"] -= 1

        # ----- REBUILD SECTIONS DICT -----
        sections.clear()
        for section in section_list:
            sections[str(section["id"])] = section

        club.page_content = json.dumps(page_content)
        club.save()

        serializer = self.get_serializer(club, context={"request": request})
        return Response(serializer.data, status=status.HTTP_200_OK)

