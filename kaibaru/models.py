from django.db import models
from accounts.models import CustomUser
from django.utils import timezone
from .storage_backends import PrivateMediaStorage
import datetime
import hashlib

from django.core.exceptions import ValidationError
from django.db.models import Q
def club_folder_upload_to(subfolder=None):

    def upload(instance, filename):
        club_subdomain = getattr(instance.club, "subdomain", "unknown_club")
        if subfolder:
            return f"{club_subdomain}/{subfolder}/{filename}"
        return f"{club_subdomain}/{filename}"
    return upload

def club_picture_upload_to(instance, filename):
    return f"{instance.subdomain}/{filename}"

def line_qr_upload_to(instance, filename):
    club_subdomain = getattr(instance, "subdomain", "unknown_club")
    return f"{club_subdomain}/line/{filename}"

def club_slate_image_upload_to(instance, filename):
    club_subdomain = getattr(instance.club, "subdomain", "unknown_club")
    return f"{club_subdomain}/slate/{filename}"  


def club_lessons_upload_to(instance, filename):
    return club_folder_upload_to("lessons")(instance, filename)

def club_member_pictures_upload_to(instance, filename):
    return club_folder_upload_to("members")(instance, filename)

def club_favicon_upload_to(instance, filename):
    club_subdomain = getattr(instance, "subdomain", "unknown_club")
    return f"{club_subdomain}/favicon/{filename}"

def club_og_image_upload_to(instance, filename):
    club_subdomain = getattr(instance, "subdomain", "unknown_club")
    return f"{club_subdomain}/og/{filename}"

class ActiveClubManager(models.Manager):
    def get_queryset(self):
        return super().get_queryset().filter(is_deleted=False)

class Club(models.Model):
    subdomain = models.SlugField(max_length=50, unique=True, null=True, blank=True)

    owner = models.ForeignKey(CustomUser, on_delete=models.PROTECT, related_name="owned_clubs")

    trial_start_date = models.DateField(null=True, blank=True)
    expiration_date = models.DateTimeField(null=True, blank=True)
    stripe_customer_id = models.CharField(max_length=255, null=True, blank=True)
    stripe_subscription_id = models.CharField(max_length=255, null=True, blank=True)

    
    subscription_active = models.BooleanField(default=False)
    last_paid_invoice_id = models.CharField(max_length=255, blank=True, null=True)
    subscription_cancel_at_period_end = models.BooleanField(default=False)
    subscription_current_period_end = models.DateTimeField(null=True, blank=True)

    stripe_anchor_date = models.PositiveSmallIntegerField(
        default=25,
        help_text="If set, subscriptions will align to this day of the month (2-27)"
    )

    # Subscription mode
    SUBSCRIPTION_MODE_CHOICES = [
        ("monthly", "月謝 (fixed monthly)"),
        ("regular", "Regular subscription"),
    ]
    subscription_mode = models.CharField(
        max_length=20,
        choices=SUBSCRIPTION_MODE_CHOICES,
        default="regular",
        help_text="Whether the club uses fixed monthly billing or regular subscription mode"
    )

    joining_fee = models.IntegerField(
        default=0,
        help_text="入会金 (one-time fee charged when a member joins)"
    )


    system = models.JSONField(null=True, blank=True)
    trial = models.JSONField(null=True, blank=True)
    contact = models.JSONField(null=True, blank=True)
    home = models.JSONField(null=True, blank=True)

    page_content = models.JSONField(null=True, blank=True)



    picture = models.ImageField(upload_to=club_picture_upload_to, null=True, blank=True)

    search_description = models.TextField(null=True, blank=True)
    title = models.CharField(max_length=200, null=True, blank=True)  
    favicon = models.ImageField(upload_to=club_favicon_upload_to, null=True, blank=True)
    og_image = models.ImageField(upload_to=club_og_image_upload_to, null=True, blank=True)


    facebook_url = models.URLField(null=True, blank=True)
    instagram_url = models.URLField(null=True, blank=True)
    line_url = models.URLField(null=True, blank=True)
    line_qr_code = models.ImageField(upload_to=line_qr_upload_to, null=True, blank=True)


    has_levels = models.BooleanField(default=False)
    has_attendance = models.BooleanField(default=False)
    
    level_names = models.JSONField(null=True, blank=True)
    
    level_milestones = models.JSONField(null=True, blank=True)

    last_reset = models.DateField(null=True, blank=True)

    is_deleted = models.BooleanField(default=False)
    deleted_at = models.DateField(null=True, blank=True)

    objects = ActiveClubManager()  
    all_objects = models.Manager()

    stripe_account_id = models.CharField(max_length=255, null=True, blank=True, unique=True)
    stripe_charges_enabled = models.BooleanField(default=False)
    stripe_payouts_enabled = models.BooleanField(default=False)
    stripe_onboarding_completed = models.BooleanField(default=False)
    stripe_details_submitted = models.BooleanField(default=False)

    def clean(self):
        super().clean()

        # Enforce 2-27 range for all modes
        if self.stripe_anchor_date:
            if self.stripe_anchor_date < 2 or self.stripe_anchor_date > 27:
                raise ValidationError(
                    {"stripe_anchor_date": "請求日は2日から27日の間で設定してください。"}
                )

        # For monthly mode, anchor is required
        if self.subscription_mode == "monthly" and not self.stripe_anchor_date:
            raise ValidationError(
                {"stripe_anchor_date": "月謝モードの場合、請求日は必須です。"}
            )

    def save(self, *args, **kwargs):
        # Set default to 25 if monthly and not set
        if not self.stripe_anchor_date:
            self.stripe_anchor_date = 25

        # Clamp the value between 2 and 27
        if self.stripe_anchor_date:
            if self.stripe_anchor_date < 2:
                self.stripe_anchor_date = 2
            elif self.stripe_anchor_date > 27:
                self.stripe_anchor_date = 27

        super().save(*args, **kwargs)

    def __str__(self):
        return self.subdomain

class MembershipPlanGroup(models.Model):
    name = models.CharField(max_length=100)
    club = models.ForeignKey(Club, on_delete=models.CASCADE)

    default_plan = models.ForeignKey(
        "MembershipPlan",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="default_for_groups"
    )

class MembershipPlan(models.Model):
    club = models.ForeignKey(Club, related_name="membership_plans", on_delete=models.CASCADE)
    
    
    group = models.ForeignKey(
        MembershipPlanGroup,
        related_name="plans",
        on_delete=models.CASCADE,
        null=True,
        blank=True
    )
    
    name = models.CharField(max_length=100)   
    description = models.TextField(blank=True)

    price = models.IntegerField()
    currency = models.CharField(max_length=10, default="jpy")
    interval = models.CharField(
        max_length=10,
        choices=[("month", "Monthly")]
    )

    stripe_price_id = models.CharField(max_length=255)
    stripe_product_id = models.CharField(max_length=255, blank=True, null=True)
    
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    max_lessons_per_month = models.PositiveIntegerField(null=True, blank=True)
    member_category = models.CharField(
        max_length=50,
        blank=True,
        help_text="e.g. junior, adult, senior"
    )
    age_min = models.PositiveIntegerField(null=True, blank=True)
    age_max = models.PositiveIntegerField(null=True, blank=True)

    bundled_plans = models.ManyToManyField(
        "self",
        symmetrical=False,
        related_name="part_of_bundles",
        blank=True,
    )

    is_deleted = models.BooleanField(default=False)
    deleted_at = models.DateTimeField(null=True, blank=True)

    apply_current_price_to_existing = models.BooleanField(
        default=False,
        help_text="If enabled, existing members will be charged the current plan price instead of their subscription price."
    )



    
    class Meta:
        ordering = ["price"]
        unique_together = ("club", "name")






class SlateImage(models.Model):
    club = models.ForeignKey(Club, on_delete=models.CASCADE, related_name="slate_images")
    created_at = models.DateTimeField(default=timezone.now)
    image = models.ImageField(upload_to=club_slate_image_upload_to)
    hash = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        db_index=True,
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["club", "hash"],
                name="unique_slate_image_per_club_hash",
            )
        ]

    def __str__(self):
        return f"{self.club.subdomain} - Image {self.id}"

class Member(models.Model):
    user = models.ForeignKey(CustomUser, null=True, blank=True, on_delete=models.SET_NULL, related_name="memberships")
    club = models.ForeignKey("Club", on_delete=models.CASCADE, null=True, blank=True, related_name="members")
    owner = models.ForeignKey(CustomUser, null=True, blank=True, on_delete=models.SET_NULL, related_name="managed_members")
    has_paid_joining_fee = models.BooleanField(default=False)
    has_been_charged_joining_fee = models.BooleanField(default=False)
    gender = models.CharField(
        max_length=10,
        choices=[("male", "Male"), ("female", "Female")],
        default="male"
    )

    birth_date = models.DateField(
        default=datetime.date(2000, 5, 4)
    )
    is_instructor = models.BooleanField(default=False)   
    is_manager = models.BooleanField(default=False)
    introduction = models.TextField(null=True, blank=True)  

    full_name = models.CharField(max_length=200)
    furigana = models.CharField(max_length=200, default="")
    phone_number = models.CharField(max_length=20, null=True, blank=True)
    emergency_number = models.CharField(max_length=20, null=True, blank=True)
    



    other_information = models.TextField(null=True, blank=True)

    picture = models.ImageField(upload_to=club_member_pictures_upload_to, null=True, blank=True)

    level = models.PositiveIntegerField(default=1)

    manual_total_participation = models.IntegerField(default=0)
    manual_level_counts = models.JSONField(default=dict)

    participation_limit = models.PositiveIntegerField(null=True, blank=True, default=None)

    is_kyukai = models.BooleanField(default=False)
    is_kyukai_paid = models.BooleanField(default=False)
    kyukai_since = models.DateField(null=True, blank=True)
    legacy_stripe_customer_id = models.CharField(max_length=255, null=True, blank=True)

    counts_for_family_discount = models.BooleanField(
        default=True,
        help_text="Whether this member counts toward family discount calculations"
    )





    class Meta:
        constraints = [
            # 1️⃣ User can only join a club once
            models.UniqueConstraint(
                fields=["user", "club"],
                name="unique_user_per_club"
            ),

            # 2️⃣ Owner-level identity uniqueness
            models.UniqueConstraint(
                fields=["club", "owner", "full_name", "birth_date"],
                name="unique_owner_member_identity"
            ),
        ]

    def __str__(self):
        return f"{self.full_name} ({self.club.subdomain})"



class OneTimeProduct(models.Model):
    club = models.ForeignKey(Club, on_delete=models.CASCADE)
    name = models.CharField(max_length=100)
    price = models.IntegerField()
    currency = models.CharField(max_length=10, default="jpy")
    stripe_price_id = models.CharField(max_length=255)
    active = models.BooleanField(default=True)

    class Meta:
        unique_together = ("club", "name")


class OneTimePayment(models.Model):
    member = models.ForeignKey(Member, on_delete=models.CASCADE)
    product = models.ForeignKey(OneTimeProduct, on_delete=models.SET_NULL, null=True)
    stripe_payment_intent_id = models.CharField(max_length=255, unique=True)
    status = models.CharField(
        max_length=20,
        choices=[
            ("pending", "Pending"),
            ("succeeded", "Succeeded"),
            ("failed", "Failed"),
            ("refunded", "Refunded"),
        ],
        default="pending",
    )
    paid_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)



class Subscription(models.Model):
    owner = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name="subscriptions")
    club = models.ForeignKey(Club, on_delete=models.CASCADE)

    stripe_subscription_id = models.CharField(max_length=255, unique=True, null=True, blank=True)

    BILLING_MODE_CHOICES = [
        ("monthly", "Fixed monthly"),
        ("regular", "Regular"),
    ]

    billing_method = models.CharField(
        max_length=20,
        choices=[
            ("stripe", "Stripe"),
            ("cash", "Cash"),
            ("bank_transfer", "Bank transfer"),
            ("manual", "Manual"),
        ],
        default="stripe"
    )

    billing_mode = models.CharField(
        max_length=20,
        choices=BILLING_MODE_CHOICES,
        default="regular",
    )

    billing_anchor_day = models.PositiveSmallIntegerField(
        help_text="Anchor day used for this subscription (1-28)",
        default="25"
    )

    STATUS_CHOICES = [
        ("active", "Active"),
        ("past_due", "Past due"),
        ("canceled", "Canceled"),
        ("unpaid", "Unpaid"),
        ("trialing", "Trialing"),
        ("pending", "Pending"),
    ]

    status = models.CharField(max_length=20, choices=STATUS_CHOICES)

    current_period_end = models.DateTimeField(null=True, blank=True)

    access_until = models.DateTimeField(null=True, blank=True)

    cancel_at_period_end = models.BooleanField(default=False)
    last_invoice_id = models.CharField(max_length=255, null=True, blank=True)

    

    

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    billing_lock_until = models.DateTimeField(null=True, blank=True)

    needs_reconciliation = models.BooleanField(default=False)
    


    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["owner", "club"],
                name="one_active_subscription_per_owner_club",
            )
        ]

    


class SubscriptionItem(models.Model):
    subscription = models.ForeignKey(
        Subscription,
        on_delete=models.CASCADE,
        related_name="items"
    )

    member = models.ForeignKey(Member, on_delete=models.CASCADE, related_name="subscription_items")

    plan = models.ForeignKey(
        MembershipPlan,
        on_delete=models.SET_NULL,
        null=True
    )
    
    price_at_subscription = models.IntegerField(null=True, blank=True)
    stripe_price_id_at_subscription = models.CharField(max_length=255, null=True, blank=True)

    deleted_at = models.DateTimeField(null=True, blank=True)
    stripe_subscription_item_id = models.CharField(max_length=255, null=True, blank=True)
    cancel_at_period_end = models.BooleanField(default=False)
    access_until = models.DateTimeField(null=True, blank=True)
    monthly_double_resume_charge_prevention = models.BooleanField(default=False)

    access_start = models.DateTimeField(null=True, blank=True)

    plan_change_locked = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)


    source_item = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="replacement_for")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["subscription", "member", "plan"],
                name="unique_member_plan_per_subscription",
            )
        ]

class SubscriptionMutation(models.Model):
    class MutationType(models.TextChoices):
        CANCEL = "cancel", "Cancel item"
        RESUME = "resume", "Resume item"
        CHANGE_PLAN = "change_plan", "Change plan"
        CANCEL_CHANGE_PLAN = "cancel_change_plan", "Cancel plan change"
        ADD_PLAN = "add_plan", "Add plan"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PROCESSING = "processing", "Processing"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"

    class InvoiceStatus(models.TextChoices):
        NOT_STARTED = "not_started", "Not started"
        RETRY = "retry", "Retry required"
        PAID = "paid", "Paid"
        FAILED = "failed", "Failed"

    subscription = models.ForeignKey(
        "Subscription",
        on_delete=models.CASCADE,
        related_name="mutations"
    )

    item = models.ForeignKey(
        "SubscriptionItem",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="mutations"
    )

    type = models.CharField(max_length=32, choices=MutationType.choices)

    # 👇 this is the key part for change_plan
    payload = models.JSONField(null=True, blank=True)

    # optional safety/debugging
    stripe_request_id = models.CharField(max_length=255, null=True, blank=True)

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True
    )

    invoice_status = models.CharField(
        max_length=20,
        choices=InvoiceStatus.choices,
        null=True,
        blank=True,
        default=None,
        db_index=True,
    )

    created_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    can_resume_until = models.DateTimeField(null=True, blank=True)

    secondary_mutation_blocked_until = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["subscription", "status"]),
            models.Index(fields=["created_at"]),
        ]


class Invoice(models.Model):

    club = models.ForeignKey(Club, on_delete=models.CASCADE)

    mutation = models.ForeignKey(
        SubscriptionMutation,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="invoices"
    )


    payer = models.ForeignKey(
        CustomUser,
        null=True,
        blank=True,
        on_delete=models.SET_NULL
    )

    payer_name = models.CharField(
        max_length=200,
        blank=True,
        null=True
    )

    payer_email = models.EmailField(
        blank=True,
        null=True
    )

    subscription = models.ForeignKey(
        Subscription,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="invoices"
    )

    STATUS_CHOICES = [
        ("draft","Draft"),
        ("open","Open"),
        ("paid","Paid"),
        ("void","Void"),
    ]

    status = models.CharField(max_length=20, choices=STATUS_CHOICES)
    
    amount_due = models.IntegerField()
    amount_paid = models.IntegerField(default=0)

    number = models.CharField(max_length=50, unique=True, blank=True, null=True)

    currency = models.CharField(max_length=10, default="jpy")

    due_date = models.DateTimeField(null=True, blank=True)

    stripe_invoice_id = models.CharField(max_length=255, null=True, blank=True)

    issued_at = models.DateTimeField(auto_now_add=True)

    billing_reason = models.CharField(
        max_length=50,
        choices=[
            ("initial_subscription", "Initial subscription"),
            ("subscription_cycle", "Subscription cycle"),
            ("add_plan", "Add plan"),
        ],
        null=True,
        blank=True,
    )

    billing_cycle_key = models.CharField(
        max_length=50,
        null=True,
        blank=True,
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["subscription", "billing_cycle_key"],
                name="unique_subscription_billing_cycle",
            ),
        ]

class InvoiceItem(models.Model):

    invoice = models.ForeignKey(
        Invoice,
        on_delete=models.CASCADE,
        related_name="items"
    )

    member = models.ForeignKey(
        Member,
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )

    description = models.CharField(max_length=255)

    amount = models.IntegerField()

    quantity = models.PositiveIntegerField(default=1)

    created_at = models.DateTimeField(auto_now_add=True)



class Payment(models.Model):

    PAYMENT_METHODS = [
        ("stripe","Stripe"),
        ("cash","Cash"),
        ("bank_transfer","Bank transfer"),
        ("manual","Manual"),
    ]

    STATUS_CHOICES = [
        ("pending","Pending"),
        ("succeeded","Succeeded"),
        ("failed","Failed"),
        ("refunded","Refunded"),
    ]

    club = models.ForeignKey(Club, on_delete=models.CASCADE)

    invoice = models.ForeignKey(
        Invoice,
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )

    method = models.CharField(max_length=20, choices=PAYMENT_METHODS)

    amount = models.IntegerField()
    currency = models.CharField(max_length=10, default="jpy")

    status = models.CharField(max_length=20, choices=STATUS_CHOICES)

    stripe_payment_intent_id = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        unique=True,
    )

    paid_at = models.DateTimeField(null=True, blank=True)

    issued_at = models.DateTimeField(auto_now_add=True)

    

class PaymentMethod(models.Model):

    METHOD_TYPES = [
        ("stripe_card","Stripe Card"),
        ("bank_transfer","Bank Transfer"),
        ("cash","Cash"),
    ]

    member = models.ForeignKey(
        Member,
        on_delete=models.CASCADE,
        related_name="payment_methods"
    )

    method_type = models.CharField(max_length=20, choices=METHOD_TYPES)

    stripe_payment_method_id = models.CharField(
        max_length=255,
        null=True,
        blank=True
    )

    is_default = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["member"],
                name="one_default_payment_method_per_member"
            )
        ]

class Lesson(models.Model):
    club = models.ForeignKey('Club', on_delete=models.CASCADE, related_name='lessons')
    instructor = models.ForeignKey('Member', on_delete=models.SET_NULL, null=True, blank=True,
                                   limit_choices_to={'is_instructor': True})
    section_id = models.IntegerField(
        null=True,
        blank=True,
        db_index=True
    )
    title = models.CharField(max_length=200, blank=True)

    weekday = models.IntegerField(choices=[(0,"月曜日"),(1,"火曜日"),(2,"水曜日"),
                                           (3,"木曜日"),(4,"金曜日"),(5,"土曜日"),(6,"日曜日")])
    start_time = models.TimeField()
    end_time = models.TimeField()

    description = models.TextField(null=True, blank=True)

    picture = models.ImageField(upload_to=club_lessons_upload_to, null=True, blank=True)

    creation_date = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.club.subdomain} - {self.get_weekday_display()} {self.start_time.strftime('%H:%M')}"

class Participation(models.Model):
    member = models.ForeignKey('Member', on_delete=models.CASCADE, related_name='participations')
    lesson = models.ForeignKey('Lesson', on_delete=models.SET_NULL, null=True, blank=True, related_name='participations')
    
    total_count = models.PositiveIntegerField(default=0)
    monthly_count = models.PositiveIntegerField(default=0)
    level_counts = models.JSONField(default=dict)
    last_participation_date = models.DateField(null=True, blank=True)
    second_last_participation_date = models.DateField(null=True, blank=True)
    
    class Meta:
        unique_together = ('member', 'lesson')  

    def __str__(self):
        lesson_title = self.lesson.title if self.lesson else "Deleted Lesson"
        return f"{self.member.full_name} - {lesson_title}"


class JoinRequest(models.Model):
    user = models.ForeignKey(CustomUser, null=True, blank=True, on_delete=models.SET_NULL, related_name="join_requests")
    club = models.ForeignKey(Club, on_delete=models.CASCADE, related_name="join_requests")
    owner = models.ForeignKey(CustomUser, null=True, blank=True, on_delete=models.SET_NULL, related_name="managed_join_requests")
    

    full_name = models.CharField(max_length=200, blank=True)
    furigana = models.CharField(max_length=200, blank=True)
    gender = models.CharField(
        max_length=10,
        choices=[("male", "Male"), ("female", "Female")],
        default="male"
    )

    birth_date = models.DateField(
        default=datetime.date(2000, 5, 4)
    )
    phone_number = models.CharField(max_length=20, blank=True)
    emergency_number = models.CharField(max_length=20, blank=True)
    picture = models.ImageField(upload_to=club_member_pictures_upload_to, null=True, blank=True)
    level = models.PositiveIntegerField(default=1)
    other_information = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    already_subscribed_plans = models.ManyToManyField(
        MembershipPlan,
        blank=True,
        related_name="legacy_join_requests",
    )

    class Meta:
        unique_together = ("user", "club")
        indexes = [
            models.Index(fields=["club"]),
            models.Index(fields=["created_at"]),
        ]

    def __str__(self):
        return f"{self.full_name} → {self.club.subdomain}"


class StripeWebhookEvent(models.Model):
    event_id = models.CharField(max_length=255, unique=True)
    STATUS_CHOICES = [
        ("processing", "Processing"),
        ("succeeded", "Succeeded"),
        ("failed", "Failed"),
    ]

    stripe_subscription_id = models.CharField(
        max_length=255,
        null=True,
        blank=True
    )

    needs_reconciliation = models.BooleanField(default=False)

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default="processing",
    )

    error = models.TextField(
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(
        null=True,
        blank=True
    )





class Discount(models.Model):
    club = models.ForeignKey(Club, on_delete=models.CASCADE, related_name="discounts")

    APPLY_TO_CHOICES = [
        ("subscription", "Monthly subscription"),
        ("joining_fee", "Joining fee"),
    ]

    apply_to = models.CharField(
        max_length=20,
        choices=APPLY_TO_CHOICES,
        default="subscription"
    )

    name = models.CharField(max_length=100)

    discount_type = models.CharField(
        max_length=20,
        choices=[("percentage", "%"), ("fixed", "¥")]
    )
    value = models.IntegerField()

    active = models.BooleanField(default=True)

    # stacking / priority (important later)
    priority = models.IntegerField(default=0)

    valid_from = models.DateTimeField(null=True, blank=True)
    valid_until = models.DateTimeField(null=True, blank=True)

    plans = models.ManyToManyField(
        "MembershipPlan",
        blank=True,
        related_name="discounts"
    )



class DiscountCondition(models.Model):
    discount = models.ForeignKey(
        Discount,
        related_name="conditions",
        on_delete=models.CASCADE
    )

    CONDITION_TYPE_CHOICES = [
        ("gender", "Gender"),
        ("age_lt", "Age <"),
        ("age_gt", "Age >"),
        ("is_family", "Family group"),
    ]

    type = models.CharField(max_length=50, choices=CONDITION_TYPE_CHOICES)

    value = models.CharField(max_length=100)

class MemberPricingAdjustment(models.Model):
    member = models.ForeignKey(
        Member,
        on_delete=models.CASCADE,
        related_name="pricing_adjustments"
    )

    club = models.ForeignKey(
        Club,
        on_delete=models.CASCADE,
        related_name="member_pricing_adjustments"
    )

    plans = models.ManyToManyField(
        MembershipPlan,
        blank=True,
        related_name="member_adjustments"
    )


    discount_type = models.CharField(
        max_length=20,
        choices=[("percentage", "%"), ("fixed", "¥")]
    )

    value = models.IntegerField()

    reason = models.TextField(blank=True)  

    active = models.BooleanField(default=True)

    valid_from = models.DateTimeField(null=True, blank=True)
    valid_until = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)


class StripeCustomer(models.Model):
    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE)
    club = models.ForeignKey(Club, on_delete=models.CASCADE)
    stripe_customer_id = models.CharField(max_length=255)

    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "club"],
                name="unique_stripe_customer_per_user_club"
            )
        ]