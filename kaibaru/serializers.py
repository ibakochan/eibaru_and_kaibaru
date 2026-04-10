from rest_framework import serializers
from .models import Member, Club, Lesson, Participation, SlateImage, JoinRequest, Subscription, SubscriptionItem

from google.cloud import storage
from django.db.models import Q
from .permissions import IsSuperuser
from django.utils import timezone

from collections import defaultdict

import json
from django.db.models import Sum
from django.db import models
from datetime import date



from .models import MembershipPlan

class SubscriptionItemSerializer(serializers.ModelSerializer):
    plan_id = serializers.IntegerField(source="plan.id", read_only=True)
    plan_name = serializers.SerializerMethodField()


    class Meta:
        model = SubscriptionItem
        fields = [
            "plan_id",
            "plan_name",
            "quantity",
            "deleted_at",
            "access_until",
            "id",
        ]

    def get_plan_name(self, obj):
        return obj.plan.name if obj.plan else None


class SubscriptionSerializer(serializers.ModelSerializer):
    items = SubscriptionItemSerializer(many=True, read_only=True)
    anchor_day = serializers.SerializerMethodField()

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
            "anchor_day",
            "billing_mode",

            "items",
        ]

    def get_anchor_day(self, obj):
        if obj.billing_anchor_day:
            return obj.billing_anchor_day
        return None

class MembershipPlanSerializer(serializers.ModelSerializer):
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
            "active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "club",
            "created_at",
            "updated_at",
        ]
    

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
    subscription = serializers.SerializerMethodField()
    age = serializers.SerializerMethodField()

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
            "is_kyukai",
            "is_kyukai_paid",
            "kyukai_since",
            "subscription",
            "is_manager",
            "is_instructor",
            "birth_date",
            "gender",
            "age",
        ]
        read_only_fields = ["id", "user", "is_manager", "is_instructor",]

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



  
    def get_subscription(self, obj):
        sub = obj.subscriptions.first()
        return SubscriptionSerializer(sub).data if sub else None
    
    def to_representation(self, instance):
        data = super().to_representation(instance)
        request = self.context.get("request")
        user = getattr(request, "user", None)

        if not user or not user.is_authenticated:
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

        club = getattr(instance, "club", None)
        user_member = club.members.filter(user=user).first() if club else None

        if instance.user == user:
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


    def get_total_participation(self, obj):
        return obj.manual_total_participation + sum(p.total_count for p in obj.participations.all())

    def get_this_month_participation(self, obj):
        return sum(p.monthly_count for p in obj.participations.all())

    def get_level_participation(self, obj):
        level_sums = defaultdict(int)

        if obj.manual_level_counts:
            for lvl, count in obj.manual_level_counts.items():
                try:
                    level_sums[int(lvl)] += int(count)
                except ValueError:
                    continue

        for p in obj.participations.all():
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

        user_member = instance.members.filter(user=user).first()

        if user_member and (user_member.is_instructor or user_member.is_manager):
            return data

        data["members"] = list(instance.members.filter(
            models.Q(is_instructor=True) | 
            models.Q(is_manager=True) | 
            models.Q(user=instance.owner) |
            models.Q(user=user) |
            models.Q(owner=user)
        ))
        data["members"] = MemberSerializer(data["members"], many=True, context=self.context).data

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

 