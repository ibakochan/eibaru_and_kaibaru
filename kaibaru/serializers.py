from rest_framework import serializers
from .models import MembershipPlanGroup, MemberPricingAdjustment, Discount, DiscountCondition, Member, Club, Lesson, Participation, SlateImage, JoinRequest, Subscription, SubscriptionItem

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
    
    def validate(self, data):
        discount_type = data.get("discount_type")
        value = data.get("value")

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
            "quantity",
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
        ]




class MembershipPlanSerializer(serializers.ModelSerializer):
    group = serializers.PrimaryKeyRelatedField(read_only=True)
    group_id = serializers.IntegerField(write_only=True, required=False, allow_null=True)
    merge_plan_id = serializers.IntegerField(write_only=True, required=False, allow_null=True)
    default_plan_id = serializers.IntegerField(write_only=True, required=False, allow_null=True)

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
        ]
        read_only_fields = [
            "id",
            "club",
            "created_at",
            "updated_at",
        ]

    def validate(self, attrs):
        bundled = attrs.get("bundled_plans", [])
    
        club = self.context.get("club")
        if not club:
            raise serializers.ValidationError({
                "club_subdomain": "Club is required."
            })
    
        # convert to set of IDs (works for both create + update)
        new_set = set(p.id for p in bundled)
    
        # ---------------------------------------------------
        # 1. OPTIONAL BUNDLE (empty = normal plan)
        # ---------------------------------------------------
        if not new_set:
            return attrs
    
        # ---------------------------------------------------
        # 2. MUST HAVE AT LEAST 2 PLANS
        # ---------------------------------------------------
        if len(new_set) < 2:
            raise serializers.ValidationError(
                {"bundled_plans": "A bundle must contain at least 2 plans."}
            )
    
        # ---------------------------------------------------
        # 3. PREVENT SELF-INCLUSION (update case)
        # ---------------------------------------------------
        if self.instance and self.instance.id in new_set:
            raise serializers.ValidationError(
                {"bundled_plans": "A plan cannot include itself in a bundle."}
            )
    
        # ---------------------------------------------------
        # 4. PREVENT NESTED BUNDLES
        # (any plan already used inside another bundle)
        # ---------------------------------------------------
        nested_bundles = MembershipPlan.objects.filter(
            id__in=new_set,
            bundled_plans__isnull=False
        ).distinct()
    
        if nested_bundles.exists():
            raise serializers.ValidationError(
                {"bundled_plans": "Bundles cannot contain other bundles."}
            )
    
        # ---------------------------------------------------
        # 5. PREVENT IDENTICAL BUNDLES (same club)
        # ---------------------------------------------------
        qs = MembershipPlan.objects.filter(club=club)
    
        if self.instance:
            qs = qs.exclude(id=self.instance.id)
    
        for p in qs:
            existing_set = set(
                p.bundled_plans.values_list("id", flat=True)
            )
    
            if existing_set == new_set:
                raise serializers.ValidationError(
                    {"bundled_plans": "An identical bundle already exists."}
                )
    
        return attrs
    


    def create(self, validated_data):
        bundled = validated_data.pop("bundled_plans", [])
        group_id = validated_data.pop("group_id", None)
        merge_plan_id = validated_data.pop("merge_plan_id", None)
        default_plan_id = validated_data.pop("default_plan_id", None)
    
        club = self.context.get("club") or validated_data.get("club")
    
        with transaction.atomic():
            plan = MembershipPlan.objects.create(**validated_data)
            group = None
    
            # CASE 1: join existing group
            if group_id:
                group = MembershipPlanGroup.objects.get(id=group_id, club=club)
                plan.group = group
                plan.save(update_fields=["group"])
    
            # CASE 2: merge with single plan → create group
            elif merge_plan_id:
                other = MembershipPlan.objects.get(id=merge_plan_id, club=club)
    
                if other.group:
                    group = other.group
                else:
                    group = MembershipPlanGroup.objects.create(club=club)
                    other.group = group
                    other.save(update_fields=["group"])
    
                plan.group = group
                plan.save(update_fields=["group"])
    
            # DEFAULT PLAN LOGIC (FIXED)
            if group:
                if default_plan_id:
                    default_plan = MembershipPlan.objects.get(
                        id=default_plan_id,
                        club=club
                    )
    
                    if default_plan.group_id != group.id:
                        raise serializers.ValidationError({
                            "default_plan_id": "Default plan must belong to the group."
                        })
    
                    group.default_plan = default_plan
                else:
                    group.default_plan = plan
    
                group.save(update_fields=["default_plan"])
    
            plan.bundled_plans.set(bundled)
            enforce_membership_plan_invariants(club)
    
        return plan
     
     
    def update(self, instance, validated_data):
        bundled = validated_data.pop("bundled_plans", None)
        group_id = validated_data.pop("group_id", None)
        merge_plan_id = validated_data.pop("merge_plan_id", None)
        default_plan_id = validated_data.pop("default_plan_id", None)
    
        if bundled is not None:
            is_current_bundle = instance.bundled_plans.exists()
    
            new_count = len(bundled)
            is_becoming_bundle = new_count >= 2
            is_becoming_normal = new_count < 2
    
            if not is_current_bundle and is_becoming_bundle:
                raise serializers.ValidationError({
                    "bundled_plans": "通常プランをセットプランに変更することはできません。"
                })
    
            if is_current_bundle and is_becoming_normal:
                raise serializers.ValidationError({
                    "bundled_plans": "セットプランは通常プランに戻すことはできません。"
                })
    
        with transaction.atomic():
    
            old_group = instance.group
    
            for attr, value in validated_data.items():
                setattr(instance, attr, value)
    
            if group_id is not None:
                if group_id == 0:
                    instance.group = None
                else:
                    instance.group = MembershipPlanGroup.objects.get(
                        id=group_id,
                        club=instance.club
                    )
    
            elif merge_plan_id:
                other = MembershipPlan.objects.get(
                    id=merge_plan_id,
                    club=instance.club
                )
    
                if other.group:
                    instance.group = other.group
                else:
                    group = MembershipPlanGroup.objects.create(club=instance.club)
                    other.group = group
                    other.save(update_fields=["group"])
                    instance.group = group
    
            else:
                instance.group = None
    
            instance.save()
    

    
            # CLEANUP: ensure default still valid
            if instance.group and instance.group.default_plan:
                if instance.group.default_plan.group_id != instance.group_id:
                    instance.group.default_plan = max(
                        instance.group.plans.all(),
                        key=lambda p: p.price,
                        default=None
                    )
                    instance.group.save(update_fields=["default_plan"])
    
            # CLEANUP groups
            if old_group:
                if old_group.plans.count() < 2:
                    old_group.plans.update(group=None)
                    old_group.delete()
    
            if bundled is not None:
                if len(bundled) >= 2:
                    instance.bundled_plans.set(bundled)

            if default_plan_id and instance.group:
                default_plan = MembershipPlan.objects.get(
                    id=default_plan_id,
                    club=instance.club
                )

                if default_plan.group_id != instance.group_id:
                    raise serializers.ValidationError({
                        "default_plan_id": "Default plan must belong to the group."
                    })

                instance.group.default_plan = default_plan
                instance.group.save(update_fields=["default_plan"])
            
        transaction.on_commit(
            lambda: enforce_membership_plan_invariants(instance.club)
        )
    
        return instance


class MembershipPlanGroupSerializer(serializers.ModelSerializer):
    plans = MembershipPlanSerializer(many=True, read_only=True)
    default_plan_id = serializers.IntegerField(required=False, allow_null=True)
    
    class Meta:
        model = MembershipPlanGroup
        fields = ["id", "plans", "default_plan_id"]


class MyJoinRequestSerializer(serializers.ModelSerializer):
    class Meta:
        model = JoinRequest
        fields = [
            "id",
            "full_name",
            "created_at",
            "owner",
            "user",
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
        ]
        read_only_fields = ["id", "user", "is_manager", "is_instructor",]

    
    def get_subscription_items(self, obj):
        items = obj._prefetched_objects_cache.get(
            "subscription_items",
            obj.subscription_items.all()
        )
        return SubscriptionItemSerializer(items, many=True).data
    
    def get_subscription_state(self, obj):
        items = obj._prefetched_objects_cache.get(
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
        ]
        read_only_fields = ["id"]

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
    membership_plans = MembershipPlanSerializer(many=True, read_only=True)
    membership_plan_groups = MembershipPlanGroupSerializer(many=True, read_only=True, source="membershipplangroup_set")




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

 