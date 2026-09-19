from rest_framework import serializers
from .models import TicketType, TicketPackage, Reservation, MembershipPlanGroup, MemberPricingAdjustment, Discount, DiscountCondition, Member, Club, Lesson, Participation, SlateImage, JoinRequest, InvoiceItem, Invoice, Subscription, SubscriptionItem

from google.cloud import storage
from django.db.models import Q
from .permissions import IsSuperuser
from django.utils import timezone
from django.utils.timezone import now

from collections import defaultdict

import json
from django.db.models import Sum
from django.db import models
from datetime import date



from .models import MembershipPlan

from django.db import transaction
from .rules_plans import enforce_membership_plan_invariants

from django.utils.timezone import now

NOW = now()

def get_visible_membership_plan_ids(club, request):
    now = timezone.now()

    # Always visible:
    # every plan that has not been deleted.
    visible_plan_ids = set(
        club.membership_plans.filter(
            is_deleted=False,
        ).values_list("id", flat=True)
    )

    if not request or not request.user.is_authenticated:
        return visible_plan_ids

    user = request.user

    # ---------------------------------------------------------
    # Deleted plans that are still in an active/grace period
    # ---------------------------------------------------------

    qs = SubscriptionItem.objects.filter(
        subscription__club=club,
        plan__is_deleted=True,
        access_until__gt=now,
    )

    # Club owner sees deleted plans still used by ANY member.
    if user.id != club.owner_id:
        # Normal user only sees deleted plans belonging to
        # their own billing/subscription owner.
        qs = qs.filter(
            subscription__owner=user,
        )

    grace_plan_ids = qs.values_list(
        "plan_id",
        flat=True,
    )

    visible_plan_ids.update(grace_plan_ids)

    return visible_plan_ids

class MemberPricingAdjustmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = MemberPricingAdjustment
        fields = [
            "id",
            "member",
            "club",
            "discount_type",
            "value",
            "reason",
            "active",
            "valid_from",
            "valid_until",
            "created_at",
            "plans",
        ]
        read_only_fields = ["id", "created_at"]

    def validate_value(self, value):
        if value < 0:
            raise serializers.ValidationError("value must be >= 0")
        return value

    def validate(self, data):
        if data["discount_type"] == "percentage" and data["value"] > 100:
            raise serializers.ValidationError("percentage cannot exceed 100")
        return data


class DiscountConditionSerializer(serializers.ModelSerializer):
    class Meta:
        model = DiscountCondition
        fields = [
            "id",
            "type",
            "value",
        ]
        read_only_fields = ["id"]

    def validate(self, data):
        ctype = data.get("type")
        value = data.get("value")
    
        if ctype == "gender":
            if value not in ["male", "female"]:
                raise serializers.ValidationError(
                    {"value": "性別は male または female のみです"}
                )
        else:
            try:
                value = int(value)  # 👈 convert here
            except (TypeError, ValueError):
                raise serializers.ValidationError(
                    {"value": "数値を入力してください"}
                )
    
            if value < 0:
                raise serializers.ValidationError(
                    {"value": "0以上である必要があります"}
                )
    
            if ctype == "plan_count_gte" and value < 2:
                raise serializers.ValidationError(
                    {"value": "プラン数は2以上である必要があります"}
                )
    
            if ctype == "is_family" and value < 1:
                raise serializers.ValidationError(
                    {"value": "家族人数は1以上である必要があります"}
                )
    
            data["value"] = value  # 👈 IMPORTANT: save converted int
    
        return data

class DiscountSerializer(serializers.ModelSerializer):
    conditions = DiscountConditionSerializer(many=True)
    plans = serializers.PrimaryKeyRelatedField(
        many=True,
        queryset=MembershipPlan.objects.all(),
        required=False
    )

    class Meta:
        model = Discount
        fields = [
            "id",
            "club",
            "name",
            "discount_type",
            "value",
            "active",
            "priority",
            "valid_from",
            "valid_until",
            "conditions",
            "apply_to",
            "plans", 
        ]
        read_only_fields = ["id", "club"]

    def get_serializer_context(self):
        context = super().get_serializer_context()

        club_id = self.request.data.get("club") or self.request.query_params.get("club")
        if club_id:
            club = Club.objects.filter(id=club_id).first()
            if club:
                context["club"] = club

        return context
    
    def validate(self, data):
        club = self.context.get("club")
        discount_type = data.get("discount_type")
        value = data.get("value")
        apply_to = data.get("apply_to")
        plans = data.get("plans", [])

        if discount_type == "percentage":
            if value > 100:
                raise serializers.ValidationError(
                    {"value": "割引率は100%以下にしてください"}
                )
            if value < 0:
                raise serializers.ValidationError(
                    {"value": "割引率は0%以上にしてください"}
                )

        elif discount_type == "fixed":
            if value < 0:
                raise serializers.ValidationError(
                    {"value": "割引額は0以上にしてください"}
                )

        if apply_to == "joining_fee" and plans:
            raise serializers.ValidationError(
                {
                    "plans":
                    "入会金割引は対象プランを指定できません。"
                    "すべてのプランに適用されます。"
                }
            )

        conditions = data.get("conditions", [])
        types = [c.get("type") for c in conditions if c.get("type")]

        if len(types) != len(set(types)):
            raise serializers.ValidationError(
                {"conditions": "同じ条件タイプは複数設定できません"}
            )

        return data

    def create(self, validated_data):


        conditions_data = validated_data.pop("conditions", [])
        plans = validated_data.pop("plans", [])
        discount = Discount.objects.create(**validated_data)

        discount.plans.set(plans)

        for cond in conditions_data:
            DiscountCondition.objects.create(discount=discount, **cond)

        return discount

    def update(self, instance, validated_data):

        conditions_data = validated_data.pop("conditions", None)
        plans = validated_data.pop("plans", None)

        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()

        if plans is not None:
            instance.plans.set(plans)

        if conditions_data is not None:
            instance.conditions.all().delete()
            for cond in conditions_data:
                DiscountCondition.objects.create(discount=instance, **cond)

        return instance



class SubscriptionItemSerializer(serializers.ModelSerializer):
    plan_id = serializers.IntegerField(source="plan.id", read_only=True)
    plan_name = serializers.SerializerMethodField()
    member_id = serializers.IntegerField(source="member.id", read_only=True)
    source_item = serializers.SerializerMethodField()
    next_item = serializers.SerializerMethodField()
    is_scheduled_change = serializers.SerializerMethodField()






    class Meta:
        model = SubscriptionItem
        fields = [
            "plan_id",
            "plan_name",
            "deleted_at",
            "access_until",
            "member_id",
            "id",
            "price_at_subscription",
            "source_item",
            "next_item",
            "is_scheduled_change",
            "access_start",
            "plan_change_locked",
        ]

    def get_source_item(self, obj):
        if not obj.source_item:
            return None

        return {
            "id": obj.source_item.id,
            "plan_id": obj.source_item.plan.id if obj.source_item.plan else None,
            "plan_name": obj.source_item.plan.name if obj.source_item.plan else None,
            "access_start": obj.source_item.access_start,
        }

    def get_next_item(self, obj):
        next_item = obj.replacement_for.first()
        if not next_item:
            return None

        return {
            "id": next_item.id,
            "plan_id": next_item.plan.id if next_item.plan else None,
            "plan_name": next_item.plan.name if next_item.plan else None,
        }
    
    def get_is_scheduled_change(self, obj):
        return obj.source_item_id is not None or obj.replacement_for.all().exists()

    def get_plan_name(self, obj):
        return obj.plan.name if obj.plan else None




class MemberSubscriptionSerializer(serializers.ModelSerializer):
    items = serializers.SerializerMethodField()

    class Meta:
        model = Subscription
        fields = [
            "id",
            "status",
            "current_period_end",
            "access_until",
            "cancel_at_period_end",
            "billing_anchor_day",
            "billing_mode",
            "items",
            "billing_method",
        ]

    def get_items(self, obj):
        items = getattr(obj, "active_subscription_items", [])
        return SubscriptionItemSerializer(items, many=True).data

class SubscriptionSerializer(serializers.ModelSerializer):
    items = SubscriptionItemSerializer(many=True, read_only=True)

    class Meta:
        model = Subscription
        fields = [
            "id",
            "status",
            "current_period_end",
            "access_until",
            "cancel_at_period_end",

            # 👇 ADD THESE
            "billing_anchor_day",
            "billing_mode",

            "items",
            "billing_method",
        ]




class MembershipPlanSerializer(serializers.ModelSerializer):
    group = serializers.PrimaryKeyRelatedField(read_only=True)
    group_id = serializers.IntegerField(
        write_only=True,
        required=False,
        allow_null=True
    )
    merge_plan_id = serializers.IntegerField(
        write_only=True,
        required=False,
        allow_null=True
    )
    default_plan_id = serializers.IntegerField(
        write_only=True,
        required=False,
        allow_null=True
    )

    class Meta:
        model = MembershipPlan
        fields = [
            "id",
            "club",
            "name",
            "description",
            "price",
            "currency",
            "interval",

            "max_lessons_per_month",
            "member_category",
            "age_min",
            "age_max",

            "bundled_plans",

            "active",
            "created_at",
            "updated_at",

            "group",
            "group_id",
            "merge_plan_id",
            "default_plan_id",

            "deleted_at",
            "apply_current_price_to_existing",

            # Plan type
            "plan_type",

            # Ticket plan configuration
            "ticket_type",
            "ticket_quantity",
            "ticket_expiration_mode",
            "ticket_expiration_days",
        ]

        read_only_fields = [
            "id",
            "club",
            "created_at",
            "updated_at",
        ]

    def validate(self, attrs):
        club = self.context.get("club")

        if not club:
            raise serializers.ValidationError({
                "club_subdomain": "Club is required."
            })

        bundled = attrs.get("bundled_plans")

        # =========================================================
        # CREATE
        # =========================================================

        if self.instance is None:

            bundled = list(bundled or [])

            # -----------------------------------------------------
            # Bundle determination
            #
            # 2+ bundled plans ALWAYS means BUNDLE.
            # The frontend does not need to send "bundle".
            # -----------------------------------------------------

            if len(bundled) >= 2:
                attrs["plan_type"] = (
                    MembershipPlan.PlanType.BUNDLE
                )

            elif len(bundled) == 1:
                raise serializers.ValidationError({
                    "bundled_plans": (
                        "A bundle must contain at least 2 plans."
                    )
                })

            else:
                plan_type = attrs.get(
                    "plan_type",
                    MembershipPlan.PlanType.NORMAL,
                )

                if plan_type not in [
                    MembershipPlan.PlanType.NORMAL,
                    MembershipPlan.PlanType.TICKET_PLAN,
                ]:
                    raise serializers.ValidationError({
                        "plan_type": "Invalid plan type."
                    })

            # -----------------------------------------------------
            # Ticket plan validation
            # -----------------------------------------------------

            plan_type = attrs.get(
                "plan_type",
                MembershipPlan.PlanType.NORMAL,
            )

            if plan_type == MembershipPlan.PlanType.TICKET_PLAN:

                if not attrs.get("ticket_type"):
                    raise serializers.ValidationError({
                        "ticket_type": (
                            "Ticket plans must specify a ticket type."
                        )
                    })

                ticket_quantity = attrs.get(
                    "ticket_quantity"
                )

                if not ticket_quantity or ticket_quantity <= 0:
                    raise serializers.ValidationError({
                        "ticket_quantity": (
                            "Ticket quantity must be greater than 0."
                        )
                    })

                expiration_mode = attrs.get(
                    "ticket_expiration_mode",
                    MembershipPlan.TicketExpirationMode.ONE_MONTH,
                )

                expiration_days = attrs.get(
                    "ticket_expiration_days"
                )

                if (
                    expiration_mode
                    == MembershipPlan.TicketExpirationMode.DAYS_AFTER_GRANT
                ):
                    if not expiration_days or expiration_days <= 0:
                        raise serializers.ValidationError({
                            "ticket_expiration_days": (
                                "Expiration days must be greater than 0."
                            )
                        })
                else:
                    attrs["ticket_expiration_days"] = None

            else:
                # Normal and bundle plans cannot have ticket config.
                attrs["ticket_type"] = None
                attrs["ticket_quantity"] = None
                attrs["ticket_expiration_mode"] = (
                    MembershipPlan.TicketExpirationMode.ONE_MONTH
                )
                attrs["ticket_expiration_days"] = None

            # -----------------------------------------------------
            # Validate bundled plans
            # -----------------------------------------------------

            if bundled:

                new_set = {
                    p.id
                    for p in bundled
                }

                if len(new_set) < 2:
                    raise serializers.ValidationError({
                        "bundled_plans": (
                            "A bundle must contain at least 2 plans."
                        )
                    })

                # All plans must belong to this club and be active.
                invalid_plans = [
                    p
                    for p in bundled
                    if (
                        p.club_id != club.id
                        or p.is_deleted
                        or not p.active
                    )
                ]

                if invalid_plans:
                    raise serializers.ValidationError({
                        "bundled_plans": (
                            "All bundled plans must belong to this "
                            "club and be active."
                        )
                    })

                # Prevent nested bundles.
                nested_bundles = MembershipPlan.objects.filter(
                    id__in=new_set,
                    bundled_plans__isnull=False
                ).distinct()

                if nested_bundles.exists():
                    raise serializers.ValidationError({
                        "bundled_plans": (
                            "Bundles cannot contain other bundles."
                        )
                    })

                # Prevent identical bundles.
                qs = MembershipPlan.objects.filter(
                    club=club
                )

                for p in qs:

                    existing_set = set(
                        p.bundled_plans.values_list(
                            "id",
                            flat=True
                        )
                    )

                    if existing_set == new_set:
                        raise serializers.ValidationError({
                            "bundled_plans": (
                                "An identical bundle already exists."
                            )
                        })

            return attrs

        # =========================================================
        # UPDATE
        # =========================================================

        submitted_plan_type = attrs.get("plan_type")

        # ---------------------------------------------------------
        # Plan type is immutable.
        # ---------------------------------------------------------

        if (
            submitted_plan_type is not None
            and submitted_plan_type != self.instance.plan_type
        ):
            raise serializers.ValidationError({
                "plan_type": (
                    "Plan type cannot be changed after creation."
                )
            })

        # ---------------------------------------------------------
        # Bundle rules are based on plan_type, NOT merely on
        # whether bundled_plans happens to contain records.
        # ---------------------------------------------------------

        if bundled is not None:

            bundled = list(bundled)

            is_bundle = (
                self.instance.plan_type
                == MembershipPlan.PlanType.BUNDLE
            )

            if is_bundle:

                if len(bundled) < 2:
                    raise serializers.ValidationError({
                        "bundled_plans": (
                            "A bundle must contain at least 2 plans."
                        )
                    })

            else:

                if len(bundled) > 0:
                    raise serializers.ValidationError({
                        "bundled_plans": (
                            "Only bundle plans can contain "
                            "bundled plans."
                        )
                    })

            # -----------------------------------------------------
            # Validate bundle contents
            # -----------------------------------------------------

            if is_bundle:

                new_set = {
                    p.id
                    for p in bundled
                }

                if self.instance.id in new_set:
                    raise serializers.ValidationError({
                        "bundled_plans": (
                            "A plan cannot include itself "
                            "in a bundle."
                        )
                    })

                invalid_plans = [
                    p
                    for p in bundled
                    if (
                        p.club_id != self.instance.club_id
                        or p.is_deleted
                        or not p.active
                    )
                ]

                if invalid_plans:
                    raise serializers.ValidationError({
                        "bundled_plans": (
                            "All bundled plans must belong to "
                            "this club and be active."
                        )
                    })

                nested_bundles = MembershipPlan.objects.filter(
                    id__in=new_set,
                    bundled_plans__isnull=False
                ).exclude(
                    id=self.instance.id
                ).distinct()

                if nested_bundles.exists():
                    raise serializers.ValidationError({
                        "bundled_plans": (
                            "Bundles cannot contain other bundles."
                        )
                    })

                qs = MembershipPlan.objects.filter(
                    club=self.instance.club
                ).exclude(
                    id=self.instance.id
                )

                for p in qs:

                    existing_set = set(
                        p.bundled_plans.values_list(
                            "id",
                            flat=True
                        )
                    )

                    if existing_set == new_set:
                        raise serializers.ValidationError({
                            "bundled_plans": (
                                "An identical bundle already exists."
                            )
                        })

        # ---------------------------------------------------------
        # Ticket-plan validation on update.
        #
        # The type itself cannot change, but ticket configuration can.
        # ---------------------------------------------------------

        if (
            self.instance.plan_type
            == MembershipPlan.PlanType.TICKET_PLAN
        ):

            ticket_type = attrs.get(
                "ticket_type",
                self.instance.ticket_type,
            )

            ticket_quantity = attrs.get(
                "ticket_quantity",
                self.instance.ticket_quantity,
            )

            expiration_mode = attrs.get(
                "ticket_expiration_mode",
                self.instance.ticket_expiration_mode,
            )

            expiration_days = attrs.get(
                "ticket_expiration_days",
                self.instance.ticket_expiration_days,
            )

            if not ticket_type:
                raise serializers.ValidationError({
                    "ticket_type": (
                        "Ticket plans must specify a ticket type."
                    )
                })

            if not ticket_quantity or ticket_quantity <= 0:
                raise serializers.ValidationError({
                    "ticket_quantity": (
                        "Ticket quantity must be greater than 0."
                    )
                })

            if (
                expiration_mode
                == MembershipPlan.TicketExpirationMode.DAYS_AFTER_GRANT
            ):

                if not expiration_days or expiration_days <= 0:
                    raise serializers.ValidationError({
                        "ticket_expiration_days": (
                            "Expiration days must be greater than 0."
                        )
                    })

            else:
                attrs["ticket_expiration_days"] = None

        else:

            # Normal/bundle plans cannot acquire ticket settings.
            if (
                attrs.get("ticket_type") is not None
                or attrs.get("ticket_quantity") is not None
                or attrs.get("ticket_expiration_days") is not None
            ):
                raise serializers.ValidationError({
                    "plan_type": (
                        "Only ticket plans can have "
                        "ticket configuration."
                    )
                })

        return attrs

    def create(self, validated_data):
        bundled = validated_data.pop(
            "bundled_plans",
            []
        )

        group_id = validated_data.pop(
            "group_id",
            None
        )

        merge_plan_id = validated_data.pop(
            "merge_plan_id",
            None
        )

        default_plan_id = validated_data.pop(
            "default_plan_id",
            None
        )

        club = (
            self.context.get("club")
            or validated_data.get("club")
        )

        # ---------------------------------------------------------
        # 2+ bundled plans ALWAYS create a BUNDLE.
        # ---------------------------------------------------------

        if len(bundled) >= 2:
            validated_data["plan_type"] = (
                MembershipPlan.PlanType.BUNDLE
            )

        with transaction.atomic():

            plan = MembershipPlan.objects.create(
                **validated_data
            )

            group = None

            # CASE 1: join existing group
            if group_id:

                group = MembershipPlanGroup.objects.get(
                    id=group_id,
                    club=club
                )

                plan.group = group

                plan.save(
                    update_fields=["group"]
                )

            # CASE 2: merge with single plan → create group
            elif merge_plan_id:

                other = MembershipPlan.objects.get(
                    id=merge_plan_id,
                    club=club
                )

                if other.group:
                    group = other.group

                else:
                    group = MembershipPlanGroup.objects.create(
                        club=club
                    )

                    other.group = group

                    other.save(
                        update_fields=["group"]
                    )

                plan.group = group

                plan.save(
                    update_fields=["group"]
                )

            # DEFAULT PLAN LOGIC
            if group:

                if default_plan_id:

                    default_plan = (
                        MembershipPlan.objects.get(
                            id=default_plan_id,
                            club=club
                        )
                    )

                    if default_plan.group_id != group.id:
                        raise serializers.ValidationError({
                            "default_plan_id": (
                                "Default plan must belong "
                                "to the group."
                            )
                        })

                    group.default_plan = default_plan

                else:

                    group.default_plan = plan

                group.save(
                    update_fields=["default_plan"]
                )

            plan.bundled_plans.set(
                bundled
            )

            enforce_membership_plan_invariants(
                club
            )

        return plan

    def update(self, instance, validated_data):

        bundled = validated_data.pop(
            "bundled_plans",
            None
        )

        group_id = validated_data.pop(
            "group_id",
            None
        )

        merge_plan_id = validated_data.pop(
            "merge_plan_id",
            None
        )

        default_plan_id = validated_data.pop(
            "default_plan_id",
            None
        )

        # ---------------------------------------------------------
        # Plan type is immutable.
        #
        # Remove it from validated_data so it can never be
        # changed by instance.save().
        # ---------------------------------------------------------

        validated_data.pop(
            "plan_type",
            None
        )

        # ---------------------------------------------------------
        # Bundle determination is based on plan_type.
        # ---------------------------------------------------------

        if bundled is not None:

            is_current_bundle = (
                instance.plan_type
                == MembershipPlan.PlanType.BUNDLE
            )

            new_count = len(bundled)

            if is_current_bundle:

                if new_count < 2:
                    raise serializers.ValidationError({
                        "bundled_plans": (
                            "セットプランは2つ以上の"
                            "プランが必要です。"
                        )
                    })

            else:

                if new_count > 0:
                    raise serializers.ValidationError({
                        "bundled_plans": (
                            "通常プランまたはチケットプランを"
                            "セットプランに変更することはできません。"
                        )
                    })

        with transaction.atomic():

            old_group = instance.group

            for attr, value in validated_data.items():
                setattr(
                    instance,
                    attr,
                    value
                )

            if group_id is not None:

                if group_id == 0:
                    instance.group = None

                else:
                    instance.group = (
                        MembershipPlanGroup.objects.get(
                            id=group_id,
                            club=instance.club
                        )
                    )

            elif merge_plan_id:

                other = MembershipPlan.objects.get(
                    id=merge_plan_id,
                    club=instance.club
                )

                if other.group:

                    instance.group = other.group

                else:

                    group = (
                        MembershipPlanGroup.objects.create(
                            club=instance.club
                        )
                    )

                    other.group = group

                    other.save(
                        update_fields=["group"]
                    )

                    instance.group = group

            else:

                instance.group = None

            instance.save()

            # CLEANUP: ensure default still valid
            if (
                instance.group
                and instance.group.default_plan
            ):

                if (
                    instance.group.default_plan.group_id
                    != instance.group_id
                ):

                    instance.group.default_plan = max(
                        instance.group.plans.all(),
                        key=lambda p: p.price,
                        default=None
                    )

                    instance.group.save(
                        update_fields=["default_plan"]
                    )

            # CLEANUP groups
            if old_group:

                if old_group.plans.count() < 2:

                    old_group.plans.update(
                        group=None
                    )

                    old_group.delete()

            if bundled is not None:

                if len(bundled) >= 2:
                    instance.bundled_plans.set(
                        bundled
                    )

            if default_plan_id and instance.group:

                default_plan = (
                    MembershipPlan.objects.get(
                        id=default_plan_id,
                        club=instance.club
                    )
                )

                if (
                    default_plan.group_id
                    != instance.group_id
                ):
                    raise serializers.ValidationError({
                        "default_plan_id": (
                            "Default plan must belong "
                            "to the group."
                        )
                    })

                instance.group.default_plan = (
                    default_plan
                )

                instance.group.save(
                    update_fields=["default_plan"]
                )

        transaction.on_commit(
            lambda: enforce_membership_plan_invariants(
                instance.club
            )
        )

        return instance


class MembershipPlanGroupSerializer(serializers.ModelSerializer):
    default_plan_id = serializers.IntegerField(required=False, allow_null=True)
    plans = serializers.SerializerMethodField()

    class Meta:
        model = MembershipPlanGroup
        fields = ["id", "plans", "default_plan_id"]

    def get_plans(self, obj):
        request = self.context.get("request")

        visible_plan_ids = get_visible_membership_plan_ids(
            obj.club,
            request,
        )

        plans = obj.plans.filter(
            id__in=visible_plan_ids,
        )

        return MembershipPlanSerializer(
            plans,
            many=True,
            context=self.context,
        ).data
    
class InvoiceItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = InvoiceItem
        fields = [
            "description",
            "amount",
        ]

class PaymentHistoryInvoiceSerializer(serializers.ModelSerializer):
    billing_member_name = serializers.SerializerMethodField()
    billing_member_furigana = serializers.SerializerMethodField()
    billing_method = serializers.SerializerMethodField()
    items = InvoiceItemSerializer(many=True, read_only=True)

    class Meta:
        model = Invoice
        fields = [
            "id",
            "billing_member_name",
            "amount_due",
            "amount_paid",
            "billing_member_furigana",
            "currency",
            "status",
            "billing_method",
            "due_date",
            "issued_at",
            "billing_reason",
            "items",
        ]

    def get_billing_member_name(self, obj):
        if not obj.payer:
            return obj.payer_name

        member = Member.objects.filter(
            user=obj.payer,
            owner=obj.payer,
            club=obj.club,
        ).first()

        if member:
            return member.full_name

        return obj.payer_name

    def get_billing_member_furigana(self, obj):
        if not obj.payer:
            return None

        member = Member.objects.filter(
            user=obj.payer,
            owner=obj.payer,
            club=obj.club,
        ).first()

        if member:
            return member.furigana

        return None

    def get_billing_method(self, obj):
        if not obj.subscription:
            return None

        return obj.subscription.billing_method

class InvoiceSerializer(serializers.ModelSerializer):
    billing_member_name = serializers.SerializerMethodField()
    billing_member_furigana = serializers.SerializerMethodField()
    billing_method = serializers.SerializerMethodField()
    billing_member_picture = serializers.SerializerMethodField()
    items = InvoiceItemSerializer(many=True, read_only=True)

    class Meta:
        model = Invoice
        fields = [
            "id",
            "billing_member_name",
            "amount_due",
            "billing_member_furigana",
            "amount_paid",
            "currency",
            "status",
            "billing_method",
            "due_date",
            "issued_at",
            "items",
            "billing_member_picture",
        ]

    def get_billing_member_picture(self, obj):
        if not obj.payer:
            return None

        member = Member.objects.filter(
            user=obj.payer,
            owner=obj.payer,
            club=obj.club,
        ).first()

        if member and member.picture:
            return member.picture.url

        return None

    def get_billing_member_name(self, obj):
        if not obj.payer:
            return obj.payer_name

        member = Member.objects.filter(
            user=obj.payer,
            owner=obj.payer,
            club=obj.club,
        ).first()

        if member:
            return member.full_name

        return obj.payer_name

    def get_billing_member_furigana(self, obj):
        if not obj.payer:
            return None

        member = Member.objects.filter(
            user=obj.payer,
            owner=obj.payer,
            club=obj.club,
        ).first()

        if member:
            return member.furigana

        return None

    def get_billing_method(self, obj):
        if not obj.subscription:
            return None

        return obj.subscription.billing_method

class MyJoinRequestSerializer(serializers.ModelSerializer):
    class Meta:
        model = JoinRequest
        fields = [
            "id",
            "full_name",
            "created_at",
            "owner",
            "user",
            "already_subscribed_plans",
        ]


class JoinRequestSerializer(serializers.ModelSerializer):
    class Meta:
        model = JoinRequest
        fields = [
            "id",
            "full_name",
            "furigana",
            "phone_number",
            "owner",
            "user",
            "emergency_number",
            "other_information",
            "picture",
            "level",
            "created_at",
            "birth_date",
            "gender",
            "already_subscribed_plans",
        ]
        read_only_fields = ["id", "status", "created_at"]

class SlateImageSerializer(serializers.ModelSerializer):

    class Meta:
        model = SlateImage
        fields = [ "created_at", "id", "image", "hash"]

class ParticipationMiniSerializer(serializers.ModelSerializer):
    class Meta:
        model = Participation
        fields = ["id", "lesson", "last_participation_date"]


class ParticipationSerializer(serializers.ModelSerializer):
    member_name = serializers.CharField(source='member.full_name', read_only=True)
    lesson_title = serializers.CharField(source='lesson.title', read_only=True)

    class Meta:
        model = Participation
        fields = [
            "id",
            "member",
            "lesson",
            "total_count",
            "monthly_count",
            "level_counts",
            "last_participation_date",
            "second_last_participation_date",
            "member_name",
            "lesson_title",
        ]
        read_only_fields = ["id", "member_name", "lesson_title"]



class MemberSerializer(serializers.ModelSerializer):
    participations = ParticipationMiniSerializer(many=True, read_only=True)
    total_participation = serializers.SerializerMethodField()
    this_month_participation = serializers.SerializerMethodField()
    level_participation = serializers.SerializerMethodField()
    age = serializers.SerializerMethodField()
    subscription_state = serializers.SerializerMethodField()
    subscription_items = serializers.SerializerMethodField()
    tickets = serializers.SerializerMethodField()

    class Meta:
        model = Member
        fields = [
            "id",
            "level",
            "user",
            "owner",
            "introduction",
            "full_name",
            "furigana",
            "phone_number",
            "emergency_number",
            "other_information",
            "picture",
            "participations",
            "total_participation",
            "this_month_participation",
            "level_participation",
            "manual_total_participation",
            "manual_level_counts",
            "participation_limit",
            "is_manager",
            "is_instructor",
            "birth_date",
            "gender",
            "age",
            "has_paid_joining_fee",
            "has_been_charged_joining_fee",
            "subscription_state",
            "subscription_items",
            "counts_for_family_discount",
            "tickets",
        ]
        read_only_fields = ["id", "user", "is_manager", "is_instructor",]

    def get_tickets(self, obj):
        grants = getattr(
            obj,
            "_prefetched_objects_cache",
            {},
        ).get(
            "ticket_grants",
            obj.ticket_grants.all(),
        )
    
        now = timezone.now()
    
        result = []
    
        for grant in grants:
            if not grant.ticket_type.active:
                continue
    
            if (
                grant.expires_at is not None
                and grant.expires_at < now
            ):
                continue
    
            used_quantity = sum(
                usage.quantity
                for usage in grant.usages.all()
                if usage.refunded_at is None
            )
    
            remaining_quantity = (
                grant.quantity - used_quantity
            )
    
            if remaining_quantity <= 0:
                continue
    
            result.append({
                "id": grant.id,
                "ticket_type_id": grant.ticket_type_id,
                "eligible_plan_ids": list(
                    grant.ticket_type.eligible_plans.values_list(
                        "id",
                        flat=True,
                    )
                ),
                "remaining_quantity": remaining_quantity,
                "expires_at": grant.expires_at,
            })
    
        return result   

    def get_subscription_items(self, obj):
        items = getattr(
            obj,
            "_prefetched_objects_cache",
            {}
        ).get(
            "subscription_items",
            obj.subscription_items.all()
        )
        return SubscriptionItemSerializer(items, many=True).data
    
    def get_subscription_state(self, obj):
        items = getattr(
            obj,
            "_prefetched_objects_cache",
            {}
        ).get(
            "subscription_items",
            obj.subscription_items.select_related("subscription").all()
        )
    
        def sort_key(x):
            if not x.subscription:
                return 0
            if not x.subscription.current_period_end:
                return 0
            return x.subscription.current_period_end
    
        item = max(items, key=sort_key, default=None)
    
        if not item or not item.subscription:
            return None
    
        sub = item.subscription
    
        return {
            "id": sub.id,
            "status": sub.status,
            "current_period_end": sub.current_period_end,
            "access_until": sub.access_until,
            "cancel_at_period_end": sub.cancel_at_period_end,
            "billing_anchor_day": sub.billing_anchor_day,
            "billing_mode": sub.billing_mode,
            "billing_method": sub.billing_method,
        }

    
            
    def get_age(self, obj):
        if not obj.birth_date:
            return None

        today = date.today()
        return (
            today.year
            - obj.birth_date.year
            - (
                (today.month, today.day)
                < (obj.birth_date.month, obj.birth_date.day)
            )
        )



    
    def to_representation(self, instance):
        data = super().to_representation(instance)
        request = self.context.get("request")
        user = getattr(request, "user", None)

        if not user or not user.is_authenticated:
            for field in [
                "subscription_state",
                "subscription_items",
                "participations",
                "total_participation",
                "this_month_participation",
                "level_participation",
                "manual_total_participation",
                "manual_level_counts",
            ]:
                data.pop(field, None)
            return data

        club = getattr(instance, "club", None)

        can_see_financials = (
            user.id == instance.user_id           # self
            or user.id == instance.owner_id       # owns this member
            or (club and club.owner_id == user.id)  # club owner
            or (club and club.members.filter(user=user, is_manager=True).exists())  # manager
        )

        if not can_see_financials:
            data.pop("subscription_state", None)
            data.pop("subscription_items", None)
    

        members = getattr(instance, "_prefetched_objects_cache", {}).get(
            "members",
            instance.club.members.all()
        )

        user_member = next((m for m in members if m.user_id == user.id), None)

        if instance.user == user:
            return data

        if instance.owner == user:
            return data

        if user_member and (user_member.is_instructor or user_member.is_manager or club.owner_id == user.id):
            return data

        for field in [
            "participations",
            "total_participation",
            "this_month_participation",
            "level_participation",
            "manual_total_participation",
            "manual_level_counts",
        ]:
            data.pop(field, None)

        return data

    def _participations(self, obj):
        return getattr(obj, "_prefetched_objects_cache", {}).get("participations", obj.participations.all())

    def get_total_participation(self, obj):
        parts = self._participations(obj)
        return obj.manual_total_participation + sum(p.total_count for p in parts)

    def get_this_month_participation(self, obj):
        parts = self._participations(obj)
        return sum(p.monthly_count for p in parts)

    def get_level_participation(self, obj):
        level_sums = defaultdict(int)
        parts = self._participations(obj)

        if obj.manual_level_counts:
            for lvl, count in obj.manual_level_counts.items():
                try:
                    level_sums[int(lvl)] += int(count)
                except ValueError:
                    continue
    
        for p in parts:
            if p.level_counts:
                for lvl, count in p.level_counts.items():
                    level_sums[int(lvl)] += count

        return dict(level_sums)


class LessonSerializer(serializers.ModelSerializer):
    instructor = serializers.SerializerMethodField()
    instructor_id = serializers.IntegerField(write_only=True, required=False, allow_null=True)
    club = serializers.PrimaryKeyRelatedField(read_only=True)

    allowed_plans = serializers.PrimaryKeyRelatedField(many=True, queryset=MembershipPlan.objects.all(), required=False)

    # NEW: Lesson background color.
    background_color = serializers.CharField(max_length=7, required=False, default="#FFFFFF")

    total_participation = serializers.SerializerMethodField()
    monthly_participation = serializers.SerializerMethodField()
    monthly_average = serializers.SerializerMethodField()


    class Meta:
        model = Lesson
        fields = [
            "id",
            "club",
            "instructor",
            "section_id",
            "instructor_id",
            "title",
            "weekday",
            "start_time",
            "end_time",
            "description",
            "picture",
            "total_participation",
            "monthly_participation",
            "monthly_average",
            "background_color",

            "allowed_plans",
            "reservation_limit",
            "reservation_only",
            "trial_price",
            "trial_disabled",


            # Visitor
            "visitor_reservation_price",
            "visitor_reservation_disabled",

            # Member
            "member_reservation_price",
            "member_reservation_disabled",
            
        ]
        read_only_fields = ["id"]

    def validate(self, attrs):
        club = (
            self.instance.club
            if self.instance
            else self.context.get("club")
        )

        if not club:
            raise serializers.ValidationError({
                "club": "Club is required."
            })

        allowed_plans = attrs.get(
            "allowed_plans",
            list(self.instance.allowed_plans.all())
            if self.instance
            else []
        )



        reservation_only = attrs.get(
            "reservation_only",
            self.instance.reservation_only
            if self.instance
            else False
        )

        reservation_limit = attrs.get(
            "reservation_limit",
            self.instance.reservation_limit
            if self.instance
            else None
        )

        invalid_plans = [
            plan
            for plan in allowed_plans
            if plan.club_id != club.id
        ]

        if invalid_plans:
            raise serializers.ValidationError({
                "allowed_plans": (
                    "All selected plans must belong to the same club "
                    "as the lesson."
                )
            })

        bundle_plans = [
            plan
            for plan in allowed_plans
            if plan.bundled_plans.exists()
        ]

        if bundle_plans:
            raise serializers.ValidationError({
                "allowed_plans": (
                    "Bundle plans cannot be used as required plans "
                    "for lessons."
                )
            })



        if (
            reservation_limit is not None
            and reservation_limit < 1
        ):
            raise serializers.ValidationError({
                "reservation_limit": (
                    "Reservation limit must be at least 1."
                )
            })


        if reservation_only and allowed_plans:
            raise serializers.ValidationError({
                "allowed_plans": (
                    "Reservation-only lessons cannot have "
                    "membership plans."
                )
            })

        start_time = attrs.get(
            "start_time",
            self.instance.start_time if self.instance else None
        )

        end_time = attrs.get(
            "end_time",
            self.instance.end_time if self.instance else None
        )

        section_id = attrs.get(
            "section_id",
            self.instance.section_id if self.instance else None
        )

        weekday = attrs.get(
            "weekday",
            self.instance.weekday if self.instance else None
        )

        if start_time is not None and end_time is not None:
            if start_time >= end_time:
                raise serializers.ValidationError({
                    "end_time": "終了時間は開始時間より後にしてください。"
                })

            overlapping_lessons = Lesson.objects.filter(
                club=club,
                section_id=section_id,
                weekday=weekday,
                start_time__lt=end_time,
                end_time__gt=start_time,
            )

            if self.instance:
                overlapping_lessons = overlapping_lessons.exclude(
                    id=self.instance.id
                )

            if overlapping_lessons.exists():
                raise serializers.ValidationError({
                    "start_time": (
                        "同じ曜日・セクションに、時間が重なるレッスンが既にあります。"
                    )
                })

        return attrs  
  
    def get_instructor(self, obj):
        if obj.instructor:
            return {"id": obj.instructor.id, "full_name": obj.instructor.full_name}
        return None
    
    def get_total_participation(self, obj):
        return obj.participations.aggregate(total=Sum("total_count"))["total"] or 0

    def get_monthly_participation(self, obj):
        return obj.participations.aggregate(total=Sum("monthly_count"))["total"] or 0


    def get_monthly_average(self, obj):
        total = self.get_total_participation(obj)
        if hasattr(obj, 'created') and obj.created:
            days = (timezone.localdate() - obj.created.date()).days
        else:
            days = 30
        months = max(days / 30, 1)  
        return round(total / months, 2)

    def update(self, instance, validated_data):
        clear_allowed_plans = self.initial_data.get(
            "clear_allowed_plans"
        )

        allowed_plans = validated_data.pop(
            "allowed_plans",
            None
        )

        instance = super().update(
            instance,
            validated_data
        )

        if clear_allowed_plans == "true":
            instance.allowed_plans.clear()
        elif allowed_plans is not None:
            instance.allowed_plans.set(allowed_plans)

        return instance




class ClubSerializer(serializers.ModelSerializer):
    members = MemberSerializer(many=True, read_only=True)
    lessons = LessonSerializer(many=True, read_only=True)
    current_user = serializers.SerializerMethodField()
    today = serializers.SerializerMethodField()
    home_images = SlateImageSerializer(many=True, read_only=True)
    home = serializers.JSONField()
    warning_message = serializers.SerializerMethodField()
    frozen = serializers.SerializerMethodField()
    slate_images = SlateImageSerializer(many=True, read_only=True)
    join_requests = serializers.SerializerMethodField()
    my_join_requests = serializers.SerializerMethodField()
    membership_plans = serializers.SerializerMethodField()
    ticket_types = serializers.SerializerMethodField()
    ticket_packages = serializers.SerializerMethodField()
    membership_plan_groups = MembershipPlanGroupSerializer(many=True, read_only=True, source="membershipplangroup_set")
    invoices = serializers.SerializerMethodField()




    class Meta:
        model = Club
        fields = [
            "id",
            "owner",
            "title",
            "subdomain",
            "members",
            "join_requests",
            "my_join_requests",
            "lessons",
            "home",
            "system",
            "trial",
            "contact",
            "picture",
            "favicon",           
            "og_image", 
            "current_user",
            "today",
            "home_images",
            "search_description",
            "has_levels",
            "has_attendance",
            "level_names",
            "level_milestones",
            "trial_start_date",
            "expiration_date",

            #gym to me stripe
         
            "subscription_active",
            "subscription_cancel_at_period_end",
            "subscription_current_period_end", 

            "subscription_mode",
            "stripe_anchor_date",
            "joining_fee",

            #member to gym stripe
            "stripe_charges_enabled",
            "stripe_payouts_enabled",
            "stripe_onboarding_completed",
            "stripe_details_submitted",
            "stripe_account_id",

            "warning_message",
            "frozen",
            "slate_images",
            "page_content",
            "membership_plans",
            "membership_plan_groups",
            "stripe_subscription_id",
            "invoices",
            "trial_price",
            "trials_disabled",
            "visitor_reservation_price",
            "visitor_reservations_disabled",

            "member_reservation_price",
            "member_reservations_disabled",

            "ticket_types",
            "ticket_packages",
        ]
        read_only_fields = [
            "id",
            "stripe_charges_enabled",
            "stripe_payouts_enabled",
            "stripe_onboarding_completed",
            "stripe_details_submitted",

            "subscription_active",
            "subscription_cancel_at_period_end",
            "subscription_current_period_end",
            "stripe_account_id",
            "stripe_subscription_id",
        ]
    
    def get_membership_plans(self, club):
        request = self.context.get("request")
    
        visible_plan_ids = get_visible_membership_plan_ids(
            club,
            request,
        )
    
        plans = club.membership_plans.filter(
            id__in=visible_plan_ids,
        )
    
        return MembershipPlanSerializer(
            plans,
            many=True,
            context=self.context,
        ).data    

    def get_ticket_types(self, club):
        return TicketTypeSerializer(
            club.ticket_types.filter(
                active=True,
            ).prefetch_related(
                "eligible_plans",
            ),
            many=True,
            context=self.context,
        ).data
    
    
    def get_ticket_packages(self, club):
        return TicketPackageSerializer(
            club.ticket_packages.filter(
                active=True,
            ).select_related(
                "ticket_type",
            ),
            many=True,
            context=self.context,
        ).data
    

    def get_frozen(self, club):  
        if not club.expiration_date:
            return False
        
        today = timezone.localdate()
        expiration_date = timezone.localtime(club.expiration_date).date()

        days_after_exp = max((today - expiration_date).days, 0)
        
 

        if not club.stripe_subscription_id:
            return days_after_exp >= 1

        return 7 < days_after_exp <= 28

    def get_warning_message(self, club):
        if not club.expiration_date:
            return None

        request = self.context.get("request")
        if not request or not request.user.is_authenticated:
            return None
        today = timezone.localdate()
        expiration_date = timezone.localtime(club.expiration_date).date()

        days_after_exp = max((today - expiration_date).days, 0)

 

        if not club.stripe_subscription_id and days_after_exp >= 7:
            return None

        if club.stripe_subscription_id and days_after_exp > 28:
            return None
        
        if not club.stripe_subscription_id:
            if request.user != club.owner:
                return None

            if days_after_exp == 0:
                return (
                    "このクラブはまだサブスクリプションに登録されていません。"
                    "本日中に支払いが完了しない場合、明日からクラブは凍結され、"
                    "編集や他のユーザーからの閲覧ができなくなります。"
                )
     
            if days_after_exp >= 1:
                days_left = max(0, 7 - days_after_exp)
                return (
                    "このクラブは現在凍結されています。"
                    "サブスクリプションが未登録のため、編集および表示が制限されています。"
                    f"あと {days_left} 日以内に支払いが完了しない場合、"
                    "クラブと所属メンバーのデータは完全に削除されます。"
                )
    
            return None
     
        if 7 < days_after_exp <= 28:
            if request.user != club.owner:
                return None
            days_left = 28 - days_after_exp
            return (
                f"このクラブは現在凍結されています。編集はできず、"
                f"オーナー以外のユーザーには表示されません。"
                f"あと {days_left} 日以内に支払いが完了しない場合、"
                f"クラブとその所属メンバーのデータは完全に削除されます。"
            )
 
        if 1 <= days_after_exp <= 7:
            if request.user == club.owner:
                days_left = 7 - days_after_exp
                days_left_till_delete = 28 - days_after_exp
                return (
                    f"クラブの有効期限が切れています。このまま支払いがない場合、"
                    f"あと {days_left} 日で編集できなくなり、他の人からも見えなくなります。"
                    f"その後 {days_left_till_delete} 日以内に支払いがない場合、クラブと所属メンバーの情報は完全に削除されます。"
                )

        return None

    def to_representation(self, instance):
        data = super().to_representation(instance)

        request = self.context.get("request")



        user = getattr(request, "user", None)

        



        if not user or not user.is_authenticated:
            members_qs = instance.members.filter(
                models.Q(is_instructor=True) |
                models.Q(is_manager=True) |
                models.Q(user=instance.owner)
            )
            data["members"] = MemberSerializer(members_qs, many=True, context=self.context).data
            return data

 
        
        if instance.owner_id == user.id:
            return data

        members = getattr(instance, "_prefetched_objects_cache", {}).get("members", instance.members.all())
        user_member = next((m for m in members if m.user_id == user.id), None)

        if user_member and (user_member.is_instructor or user_member.is_manager):
            return data


        filtered = [
            m for m in members
            if m.is_instructor
            or m.is_manager
            or m.user_id == instance.owner_id
            or m.user_id == user.id
            or m.owner_id == user.id
        ]
        data["members"] = MemberSerializer(filtered, many=True, context=self.context).data

        return data



    def get_current_user(self, obj):
        request = self.context.get("request")
        if request and request.user.is_authenticated:
            return {
                "id": request.user.id,
                "username": request.user.username,
                "email": request.user.email,
            }
        return None

    def get_today(self, obj):
        return timezone.localdate().isoformat()

    

    def get_my_join_requests(self, club):
        request = self.context.get("request")

        if not request or not request.user.is_authenticated:
            return []

        qs = club.join_requests.filter(
            Q(user=request.user) | Q(owner=request.user)
        ).order_by("-created_at")

        return MyJoinRequestSerializer(qs, many=True).data

    def get_invoices(self, club):
        request = self.context.get("request")

        if not request or not request.user.is_authenticated:
            return []

        if request.user.id != club.owner_id:
            return []

        qs = Invoice.objects.filter(
            club=club,
            status="open",
            subscription__billing_method__in=[
                "cash",
                "manual",
                "bank_transfer",
            ],
        ).select_related(
            "subscription",
        )

        return InvoiceSerializer(
            qs,
            many=True,
            context=self.context
        ).data

    def get_join_requests(self, club):
        request = self.context.get("request")

        if not request or not request.user.is_authenticated:
            return []

        if request.user.id != club.owner_id:
            return []

        qs = club.join_requests.order_by("-created_at")

        return JoinRequestSerializer(
            qs,
            many=True,
            context=self.context
        ).data

 

class TicketTypeSerializer(serializers.ModelSerializer):
    eligible_plans = serializers.PrimaryKeyRelatedField(
        many=True,
        queryset=MembershipPlan.objects.all(),
        required=True,
    )

    class Meta:
        model = TicketType
        fields = [
            "id",
            "club",
            "name",
            "description",
            "eligible_plans",
            "active",
            "created_at",
        ]
        read_only_fields = [
            "id",
            "club",
            "created_at",
        ]

    def validate_name(self, value):
        value = value.strip()

        if not value:
            raise serializers.ValidationError(
                "チケット名を入力してください。"
            )

        return value

    def validate(self, attrs):
        club = (
            self.instance.club
            if self.instance
            else self.context.get("club")
        )

        if not club:
            raise serializers.ValidationError({
                "club_subdomain": "Club is required."
            })

        eligible_plans = attrs.get(
            "eligible_plans",
            list(self.instance.eligible_plans.all())
            if self.instance
            else []
        )

        if not eligible_plans:
            raise serializers.ValidationError({
                "eligible_plans": (
                    "少なくとも1つの対象プランを選択してください。"
                )
            })

        invalid_plans = [
            plan
            for plan in eligible_plans
            if (
                plan.club_id != club.id
                or plan.is_deleted
                or not plan.active
            )
        ]

        if invalid_plans:
            raise serializers.ValidationError({
                "eligible_plans": (
                    "対象プランには同じクラブの有効なプランのみ "
                    "指定できます。"
                )
            })

        return attrs

class TicketPackageSerializer(serializers.ModelSerializer):
    ticket_type_name = serializers.CharField(
        source="ticket_type.name",
        read_only=True,
    )

    class Meta:
        model = TicketPackage
        fields = [
            "id",
            "club",
            "ticket_type",
            "ticket_type_name",
            "name",
            "description",
            "quantity",
            "price",
            "currency",
            "stripe_price_id",
            "active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "club",
            "stripe_price_id",
            "created_at",
            "updated_at",
        ]

    def validate_name(self, value):
        value = value.strip()

        if not value:
            raise serializers.ValidationError(
                "パッケージ名を入力してください。"
            )

        return value

    def validate_quantity(self, value):
        if value <= 0:
            raise serializers.ValidationError(
                "チケット枚数は1以上にしてください。"
            )

        return value

    def validate_price(self, value):
        if value <= 0:
            raise serializers.ValidationError(
                "価格は1円以上にしてください。"
            )

        return value

    def validate(self, attrs):
        club = (
            self.instance.club
            if self.instance
            else self.context.get("club")
        )

        if not club:
            raise serializers.ValidationError({
                "club_subdomain": "Club is required."
            })

        ticket_type = attrs.get(
            "ticket_type",
            self.instance.ticket_type
            if self.instance
            else None,
        )

        if not ticket_type:
            raise serializers.ValidationError({
                "ticket_type": "チケットタイプを選択してください。"
            })

        # -----------------------------------------
        # Same club
        # -----------------------------------------

        if ticket_type.club_id != club.id:
            raise serializers.ValidationError({
                "ticket_type": (
                    "チケットタイプは同じクラブに所属している必要があります。"
                )
            })

        # -----------------------------------------
        # Ticket type must be active
        # -----------------------------------------

        if not ticket_type.active:
            raise serializers.ValidationError({
                "ticket_type": (
                    "このチケットタイプは現在無効です。"
                )
            })

        # -----------------------------------------
        # Ticket type cannot be changed on update
        # -----------------------------------------

        if (
            self.instance
            and ticket_type.id != self.instance.ticket_type_id
        ):
            raise serializers.ValidationError({
                "ticket_type": (
                    "チケットパッケージの対象チケットタイプは "
                    "変更できません。"
                )
            })

        # -----------------------------------------
        # Currency
        # -----------------------------------------

        currency = attrs.get(
            "currency",
            self.instance.currency
            if self.instance
            else "jpy",
        )

        if currency.lower() != "jpy":
            raise serializers.ValidationError({
                "currency": "現在はJPYのみ対応しています。"
            })

        return attrs

class ReservationSerializer(serializers.ModelSerializer):
    lesson_title = serializers.CharField(
        source="lesson.title",
        read_only=True,
    )

    lesson_weekday = serializers.IntegerField(
        source="lesson.weekday",
        read_only=True,
    )

    lesson_start_time = serializers.TimeField(
        source="lesson.start_time",
        read_only=True,
    )

    lesson_end_time = serializers.TimeField(
        source="lesson.end_time",
        read_only=True,
    )

    instructor = serializers.SerializerMethodField()

    class Meta:
        model = Reservation
        fields = [
            "id",

            # Lesson information
            "lesson",
            "lesson_title",
            "lesson_weekday",
            "lesson_start_time",
            "lesson_end_time",
            "instructor",

            # Ownership
            "club",
            "member",
            "user",

            # Reservation
            "reservation_type",
            "status",
            "reservation_date",

            # Customer information
            "full_name",
            "email",
            "phone_number",

            # Payment
            "amount",
            "currency",
            "paid_at",

            # Metadata
            "created_at",
        ]

        read_only_fields = fields

    def get_instructor(self, obj):
        instructor = obj.lesson.instructor

        if not instructor:
            return None

        return {
            "id": instructor.id,
            "full_name": instructor.full_name,
        }


