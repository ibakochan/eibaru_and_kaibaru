from django.db import models
from accounts.models import CustomUser
from django.utils import timezone
from .storage_backends import PrivateMediaStorage
import datetime
import hashlib

from django.core.exceptions import ValidationError
from django.db.models import Q
from django.core.validators import RegexValidator


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


def club_news_upload_to(instance, filename):
    import os
    import uuid
    club_subdomain = getattr(instance.club, "subdomain", "unknown_club")
    ext = os.path.splitext(filename or "")[1].lower()
    if ext not in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
        ext = ".jpg"
    return f"{club_subdomain}/news/{uuid.uuid4().hex}{ext}"


def club_video_upload_to(instance, filename):
    import os
    import uuid
    club_subdomain = getattr(instance.club, "subdomain", "unknown_club")
    ext = os.path.splitext(filename or "")[1].lower()
    if ext not in {".mp4", ".webm"}:
        ext = ".mp4"
    return f"{club_subdomain}/video/{uuid.uuid4().hex}{ext}"  


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

def club_expense_receipt_upload_to(instance, filename):
    import os
    import uuid

    ext = os.path.splitext(filename or "")[1].lower()
    if ext not in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
        ext = ""
    club_subdomain = getattr(getattr(instance, "club", None), "subdomain", "unknown_club")
    return f"{club_subdomain}/expenses/{uuid.uuid4().hex}{ext}"

class ActiveClubManager(models.Manager):
    def get_queryset(self):
        return super().get_queryset().filter(is_deleted=False)

class Club(models.Model):
    subdomain = models.SlugField(max_length=50, unique=True, null=True, blank=True)
    custom_domain = models.CharField(
        max_length=253,
        null=True,
        blank=True,
        unique=True,
        help_text="Hostname the owner attached. Empty until they save one.",
    )
    custom_domain_status = models.CharField(
        max_length=20,
        blank=True,
        default="",
        help_text="pending until DNS points here and a certificate is issued. active once it is live.",
    )
    scheduled_for_domain_cert = models.BooleanField(
        default=False,
        db_index=True,
        help_text="True after the owner saves a domain, until the hourly task finishes the certificate.",
    )

    owner = models.ForeignKey(CustomUser, on_delete=models.PROTECT, related_name="owned_clubs")

    trial_start_date = models.DateField(null=True, blank=True)
    expiration_date = models.DateTimeField(null=True, blank=True)
    stripe_customer_id = models.CharField(max_length=255, null=True, blank=True)
    stripe_subscription_id = models.CharField(max_length=255, null=True, blank=True)

    trials_disabled = models.BooleanField(default=False, help_text="If enabled, trial reservations are unavailable for all lessons.")

    visitor_reservations_disabled = models.BooleanField(default=False, help_text="If enabled, visitor reservations are unavailable for all lessons.")

    member_reservations_disabled = models.BooleanField(default=False, help_text="If enabled, member reservations are unavailable for all lessons.")

    member_reservation_max_count = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Default maximum upcoming member reservations per member per lesson. Empty means unlimited. Lessons may override.",
    )
    member_reservation_max_days_ahead = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="How many days ahead a member may reserve. Empty means no date horizon. 0 means today only.",
    )
    visitor_reservation_max_count = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Default maximum upcoming visitor reservations per email per lesson. Empty means unlimited. Lessons may override.",
    )
    visitor_reservation_max_days_ahead = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="How many days ahead a visitor may reserve. Empty means no date horizon. 0 means today only.",
    )

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
    default_instructor_lesson_fee = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Default yen paid to each instructor per lesson taught. Lessons may override.",
    )
    SERVICE_SLOT_STEP_CHOICES = (
        (15, "15 minutes"),
        (30, "30 minutes"),
        (60, "60 minutes"),
    )
    service_slot_step_minutes = models.PositiveSmallIntegerField(
        choices=SERVICE_SLOT_STEP_CHOICES,
        default=15,
        help_text="Booking start interval for service_schedule (15 / 30 / 60).",
    )

    JOIN_PICTURE_MODE_CHOICES = [
        ("off", "Optional"),
        ("all", "Every join request"),
        ("age", "Join requests at or above an age"),
    ]
    join_picture_mode = models.CharField(
        max_length=10,
        choices=JOIN_PICTURE_MODE_CHOICES,
        default="off",
        help_text="When a join request must include a photo. Off means the photo is optional.",
    )
    join_picture_min_age = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        help_text="Used when join_picture_mode is age. A photo is required at this age and older.",
    )
    join_terms = models.TextField(
        blank=True,
        default="",
        help_text="Terms a person must accept to request membership. Empty means there are no terms.",
    )


    system = models.JSONField(null=True, blank=True)
    trial = models.JSONField(null=True, blank=True)
    contact = models.JSONField(null=True, blank=True)
    home = models.JSONField(null=True, blank=True)

    page_content = models.JSONField(null=True, blank=True)

    LAYOUT_CHOICES = [
        ("default", "Default"),
        ("yoga", "Yoga"),
        ("striking", "Striking"),
        ("bjj/grappling", "BJJ/Grappling"),
        ("karate/judo", "Karate/Judo"),
        ("boxercise/fitness", "Boxercise/Fitness"),
        ("regular_gym", "Regular Gym"),
        ("cramschool", "Cramschool"),
        ("eikaiwa_school", "Eikaiwa School"),
        ("dance", "Dance"),
        ("swimming", "Swimming"),
        ("music", "Music"),
        ("regular_sports", "Regular Sports"),
        ("hair_salon", "Hair salon"),
        ("nail_salon", "Nail salon"),
    ]
    layout = models.CharField(
        max_length=20,
        choices=LAYOUT_CHOICES,
        default="default",
        help_text="Public page look. Default uses the standard layout.",
    )

    default_background_color = models.CharField(
        max_length=7,
        null=True,
        blank=True,
        help_text="Page background as #RRGGBB. Empty follows the layout preset. Any hex is allowed.",
    )



    picture = models.ImageField(upload_to=club_picture_upload_to, null=True, blank=True)

    search_description = models.TextField(null=True, blank=True)
    title = models.CharField(max_length=200, null=True, blank=True)  
    favicon = models.ImageField(upload_to=club_favicon_upload_to, null=True, blank=True)
    og_image = models.ImageField(upload_to=club_og_image_upload_to, null=True, blank=True)


    facebook_url = models.URLField(null=True, blank=True)
    instagram_url = models.URLField(null=True, blank=True)
    line_url = models.URLField(null=True, blank=True)
    line_qr_code = models.ImageField(upload_to=line_qr_upload_to, null=True, blank=True)


    has_attendance = models.BooleanField(default=False)

    last_reset = models.DateField(null=True, blank=True)

    is_deleted = models.BooleanField(default=False)
    deleted_at = models.DateField(null=True, blank=True)

    objects = ActiveClubManager()  
    all_objects = models.Manager()

    stripe_account_id = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        help_text="Copy of the owner's platform subscription connected account.",
    )
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


class PlatformSubscription(models.Model):
    """One Kaibaru subscription for a club owner. Not a member plan."""

    user = models.OneToOneField(
        CustomUser,
        on_delete=models.CASCADE,
        related_name="platform_subscription",
    )
    stripe_customer_id = models.CharField(max_length=255, null=True, blank=True)
    stripe_subscription_id = models.CharField(
        max_length=255,
        unique=True,
        null=True,
        blank=True,
    )
    status = models.CharField(max_length=20, default="pending")
    current_period_end = models.DateTimeField(null=True, blank=True)
    cancel_at_period_end = models.BooleanField(default=False)
    last_paid_invoice_id = models.CharField(max_length=255, null=True, blank=True)
    stripe_account_id = models.CharField(max_length=255, null=True, blank=True, unique=True)
    stripe_charges_enabled = models.BooleanField(default=False)
    stripe_payouts_enabled = models.BooleanField(default=False)
    stripe_onboarding_completed = models.BooleanField(default=False)
    stripe_details_submitted = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"platform subscription user={self.user_id}"


class PlatformClub(models.Model):
    """A club covered by its owner's platform subscription."""

    platform_subscription = models.ForeignKey(
        PlatformSubscription,
        on_delete=models.CASCADE,
        related_name="clubs",
    )
    club = models.OneToOneField(
        Club,
        on_delete=models.CASCADE,
        related_name="platform_club",
    )
    awaiting_checkout = models.BooleanField(default=False)
    pending_removal = models.BooleanField(default=False)
    removal_kind = models.CharField(max_length=20, blank=True, default="")
    access_until = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"platform club={self.club_id}"


class ClubSubscriptionMutation(models.Model):
    class MutationType(models.TextChoices):
        ADD_CLUB = "add_club", "Add club"
        REMOVE_CLUB = "remove_club", "Remove club"
        RESUME_CLUB = "resume_club", "Resume club"
        CANCEL_SUBSCRIPTION = "cancel_subscription", "Cancel subscription"
        RESUME_SUBSCRIPTION = "resume_subscription", "Resume subscription"
        SYNC_MEMBERS = "sync_members", "Sync members"
        MEMBER_QUANTITY = "member_quantity", "Member quantity"
        SYNC_WORKERS = "sync_workers", "Sync workers"
        WORKER_QUANTITY = "worker_quantity", "Worker quantity"
        SYNC_DOMAIN = "sync_domain", "Sync domain"
        DELETE_CLUB = "delete_club", "Delete club"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PROCESSING = "processing", "Processing"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"

    platform_subscription = models.ForeignKey(
        PlatformSubscription,
        on_delete=models.CASCADE,
        related_name="mutations",
    )
    club = models.ForeignKey(
        Club,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="platform_mutations",
    )
    type = models.CharField(max_length=32, choices=MutationType.choices)
    payload = models.JSONField(null=True, blank=True)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    local_state_applied = models.BooleanField(default=False)
    stripe_finished = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["platform_subscription", "status"]),
            models.Index(fields=["created_at"]),
        ]


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
    
    class PlanType(models.TextChoices):
        NORMAL = "normal", "Normal"
        TICKET_PLAN = "ticket_plan", "Ticket plan"
        BUNDLE = "bundle", "Bundle"

    group = models.ForeignKey(
        MembershipPlanGroup,
        related_name="plans",
        on_delete=models.CASCADE,
        null=True,
        blank=True
    )

    plan_type = models.CharField(
        max_length=20,
        choices=PlanType.choices,
        default=PlanType.NORMAL,
        db_index=True,
    )

    # Ticket-plan configuration
    ticket_type = models.ForeignKey(
        "TicketType",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="ticket_plans",
    )

    ticket_quantity = models.PositiveIntegerField(
        null=True,
        blank=True,
    )

    class TicketExpirationMode(models.TextChoices):
        ONE_MONTH = "one_month", "One month"
        NEVER = "never", "Never"
        DAYS_AFTER_GRANT = "days_after_grant", "Days after grant"

    ticket_expiration_mode = models.CharField(
        max_length=30,
        choices=TicketExpirationMode.choices,
        default=TicketExpirationMode.ONE_MONTH,
    )

    ticket_expiration_days = models.PositiveIntegerField(
        null=True,
        blank=True,
    )
    
    # NULL after deletion so the club can create another plan with the
    # same name. MySQL allows many (club, NULL) rows under the unique key.
    # The name members still see is name_before_deleted.
    name = models.CharField(max_length=100, null=True, blank=True)
    name_before_deleted = models.CharField(max_length=100, blank=True, default="")
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
    age_min = models.PositiveIntegerField(null=True, blank=True)
    age_max = models.PositiveIntegerField(null=True, blank=True)
    allowed_gender = models.CharField(
        max_length=10,
        choices=[("male", "Male"), ("female", "Female")],
        null=True,
        blank=True,
        help_text="If set, only this gender may join. Empty means any gender.",
    )

    bundled_plans = models.ManyToManyField(
        "self",
        symmetrical=False,
        related_name="part_of_bundles",
        blank=True,
    )

    is_deleted = models.BooleanField(default=False)
    deleted_at = models.DateTimeField(null=True, blank=True)

    # An owner has requested deletion of this plan and the system is
    # currently working through cancellation of existing subscribers.
    #
    # State model:
    #   scheduled_for_deletion=False, is_deleted=False -> normal active plan
    #   scheduled_for_deletion=True,  is_deleted=False -> deletion in progress
    #   is_deleted=True                                -> plan is actually deleted (terminal)
    #
    # A plan with either flag set must never gain a new/reactivated
    # active SubscriptionItem.
    scheduled_for_deletion = models.BooleanField(default=False)

    apply_current_price_to_existing = models.BooleanField(
        default=False,
        help_text="If enabled, existing members will be charged the current plan price instead of their subscription price."
    )



    
    def visible_name(self):
        return self.name or self.name_before_deleted or ""

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


class ClubNews(models.Model):
    club = models.ForeignKey(Club, on_delete=models.CASCADE, related_name="news_posts")
    title = models.CharField(max_length=120)
    published_on = models.DateField(default=datetime.date.today)
    summary = models.CharField(max_length=200, blank=True)
    body = models.TextField(blank=True)
    image = models.ImageField(upload_to=club_news_upload_to, null=True, blank=True)
    youtube_url = models.URLField(max_length=300, blank=True)
    author_name = models.CharField(max_length=80, blank=True)
    link_url = models.URLField(max_length=300, blank=True)
    link_label = models.CharField(max_length=40, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-published_on", "-id"]

    def __str__(self):
        return f"{self.club.subdomain} - {self.title}"


class ClubVideo(models.Model):
    club = models.OneToOneField(Club, on_delete=models.CASCADE, related_name="hero_video")
    created_at = models.DateTimeField(default=timezone.now)
    file = models.FileField(upload_to=club_video_upload_to)

    def __str__(self):
        return f"{self.club.subdomain} - video"

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
    is_worker = models.BooleanField(
        default=False,
        help_text="Salon staff who take course bookings. Not a membership-plan member.",
    )
    is_manager = models.BooleanField(default=False)
    introduction = models.TextField(null=True, blank=True)  

    full_name = models.CharField(max_length=200)
    furigana = models.CharField(max_length=200, default="")
    phone_number = models.CharField(max_length=20, null=True, blank=True)
    emergency_number = models.CharField(max_length=20, null=True, blank=True)
    



    other_information = models.TextField(null=True, blank=True)

    picture = models.ImageField(upload_to=club_member_pictures_upload_to, null=True, blank=True)
    accepted_join_terms = models.TextField(
        blank=True,
        default="",
        help_text="The join terms this member accepted. Empty if the club had none.",
    )

    manual_total_participation = models.IntegerField(default=0)

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


class MemberTransfer(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        ACCEPTED = "accepted", "Accepted"
        CANCELED = "canceled", "Canceled"

    member = models.ForeignKey(
        Member,
        on_delete=models.CASCADE,
        related_name="transfers",
    )
    from_owner = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        related_name="member_transfers_sent",
    )
    to_user = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        related_name="member_transfers_received",
    )
    token = models.CharField(max_length=64, unique=True)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    accepted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["member", "status"], name="kaibaru_mem_member_status_idx"),
        ]


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

    stripe_status_checked_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
    )
    


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
        CASH_TO_STRIPE = "cash_to_stripe", "Cash to Stripe"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PROCESSING = "processing", "Processing"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"

    class InvoiceStatus(models.TextChoices):
        OPEN = "open", "Invoice open"
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

    local_state_applied = models.BooleanField(
        default=False,
    )

    stripe_reconciliation_finished = models.BooleanField(
        default=False,
    )

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

    PAYMENT_METHOD_CHOICES = [
        ("stripe", "Stripe"),
        ("cash", "Cash"),
        ("bank_transfer", "Bank transfer"),
    ]

    payment_method = models.CharField(
        max_length=20,
        choices=PAYMENT_METHOD_CHOICES,
        default="cash",
    )

    original_payment_method = models.CharField(
        max_length=20,
        choices=PAYMENT_METHOD_CHOICES,
        default="cash",
    )


    stripe_payment_failure_count = models.PositiveIntegerField(
        default=0,
    )

    stripe_payment_failed_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    stripe_payment_failure_email_sent = models.BooleanField(
        default=False,
    )

    stripe_cash_member_email_sent = models.BooleanField(
        default=False,
    )

    stripe_cash_owner_email_sent = models.BooleanField(
        default=False,
    )

    STRIPE_CASH_TRANSITION_STATUS_CHOICES = [
        ("started", "Started"),
        ("succeeded", "Succeeded"),
    ]
    
    stripe_cash_transition_status = models.CharField(
        max_length=20,
        choices=STRIPE_CASH_TRANSITION_STATUS_CHOICES,
        null=True,
        blank=True,
    )
    
    amount_due = models.IntegerField()
    amount_paid = models.IntegerField(default=0)
    net_yen = models.IntegerField(
        null=True,
        blank=True,
        help_text="Yen that reached the club after Stripe's fee. Set when the invoice is paid.",
    )

    number = models.CharField(max_length=50, unique=True, blank=True, null=True)

    currency = models.CharField(max_length=10, default="jpy")

    due_date = models.DateTimeField(null=True, blank=True)

    stripe_invoice_id = models.CharField(max_length=255, null=True, blank=True)

    issued_at = models.DateTimeField(auto_now_add=True)

    converted_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When a Stripe invoice was switched to cash. The platform fee uses this month instead of issued_at.",
    )

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

    platform_fee_applied = models.BooleanField(default=False)

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


RESERVATION_PAYMENT_OPTION_CHOICES = [
    ("stripe", "Card (Stripe)"),
    ("cash", "Pay at the venue"),
    ("both", "Card or pay at the venue"),
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

    background_color = models.CharField(
        max_length=7,
        default="#FFFFFF",
        validators=[
            RegexValidator(
                regex=r"^#[0-9A-Fa-f]{6}$",
                message="カラーは #FFFFFF の形式で指定してください。"
            )
        ]
    )

    # NEW
    # If empty => anybody can attend
    allowed_plans = models.ManyToManyField(
        "MembershipPlan",
        blank=True,
        related_name="allowed_lessons"
    )

    reservation_only = models.BooleanField(default=False, help_text="If enabled, this lesson can only be booked through a reservation.")

    # --------------------------------------------------------
    # Reservation settings
    # --------------------------------------------------------

    # Maximum number of reservations for this lesson.
    # NULL = unlimited.
    reservation_limit = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Maximum number of reservations. Leave empty for unlimited."
    )

    # Prevent trial reservations for this lesson entirely.
    trial_disabled = models.BooleanField(
        default=False,
        help_text="If enabled, trial reservations are not allowed."
    )

    # How a priced reservation for this lesson may be paid. Clubs without
    # a connected Stripe account can only take payment at the venue.
    reservation_payment_options = models.CharField(
        max_length=10,
        choices=RESERVATION_PAYMENT_OPTION_CHOICES,
        default="stripe",
        help_text="stripe, cash (pay at the venue), or both.",
    )
    # False: this lesson offers every club reservation price.
    # True: it offers only the club prices it selected, plus its own.
    reservation_menu_customized = models.BooleanField(default=False)

    # Explicitly disables visitor reservations for this lesson.
    visitor_reservation_disabled = models.BooleanField(
        default=False,
        help_text="If enabled, visitor reservations are not allowed for this lesson."
    )

    member_reservation_disabled = models.BooleanField(
        default=False,
        help_text="If enabled, member reservations are not allowed for this lesson."
    )

    member_reservation_max_count = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Maximum upcoming member reservations for this lesson. Empty uses the club setting.",
    )
    member_reservation_max_days_ahead = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="How many days ahead a member may reserve this lesson. Empty uses the club setting.",
    )
    visitor_reservation_max_count = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Maximum upcoming visitor reservations for this lesson per email. Empty uses the club setting.",
    )
    visitor_reservation_max_days_ahead = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="How many days ahead a visitor may reserve this lesson. Empty uses the club setting.",
    )

    age_min = models.PositiveIntegerField(null=True, blank=True)
    age_max = models.PositiveIntegerField(null=True, blank=True)
    allowed_gender = models.CharField(
        max_length=10,
        choices=[("male", "Male"), ("female", "Female")],
        null=True,
        blank=True,
        help_text="If set, only this gender may reserve. Empty means any gender.",
    )
    instructor_fee = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Yen paid to each instructor for this lesson. Empty uses the club default.",
    )
    default_instructors = models.ManyToManyField(
        "Member",
        blank=True,
        related_name="default_lessons",
        through="LessonInstructor",
        limit_choices_to={"is_instructor": True},
    )

    creation_date = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.club.subdomain} - {self.get_weekday_display()} {self.start_time.strftime('%H:%M')}"


class LessonInstructor(models.Model):
    """Default instructors assigned to a recurring lesson (can be several)."""

    lesson = models.ForeignKey(
        Lesson,
        on_delete=models.CASCADE,
        related_name="lesson_instructors",
    )
    instructor = models.ForeignKey(
        Member,
        on_delete=models.CASCADE,
        related_name="lesson_instructor_links",
        limit_choices_to={"is_instructor": True},
    )
    position = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["position", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["lesson", "instructor"],
                name="unique_lesson_default_instructor",
            ),
        ]

    def clean(self):
        super().clean()
        if self.instructor_id and self.lesson_id:
            if self.instructor.club_id != self.lesson.club_id:
                raise ValidationError(
                    {"instructor": "この指導者はこのクラブの会員ではありません。"}
                )
            if not self.instructor.is_instructor:
                raise ValidationError(
                    {"instructor": "会員を指導者にしてから設定してください。"}
                )

    def __str__(self):
        return f"{self.lesson_id}:{self.instructor_id}"


class LessonDateCancel(models.Model):
    """Marks one calendar date of a recurring lesson as canceled."""

    lesson = models.ForeignKey(
        Lesson,
        on_delete=models.CASCADE,
        related_name="date_cancels",
    )
    date = models.DateField()
    note = models.CharField(max_length=200, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["lesson", "date"],
                name="unique_lesson_date_cancel",
            ),
        ]
        indexes = [
            models.Index(fields=["lesson", "date"]),
        ]

    def __str__(self):
        return f"{self.lesson_id} canceled {self.date}"


class LessonDateStaff(models.Model):
    """Replacement instructors for one date of a recurring lesson.

    When this row exists, it replaces the lesson's default instructors for
    that date. An empty instructor list means nobody is assigned that day,
    but the lesson is not canceled unless LessonDateCancel exists.
    """

    lesson = models.ForeignKey(
        Lesson,
        on_delete=models.CASCADE,
        related_name="date_staffs",
    )
    date = models.DateField()
    note = models.CharField(max_length=200, blank=True, default="")
    instructors = models.ManyToManyField(
        Member,
        blank=True,
        related_name="lesson_date_staff_assignments",
        through="LessonDateStaffMember",
        limit_choices_to={"is_instructor": True},
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["lesson", "date"],
                name="unique_lesson_date_staff",
            ),
        ]
        indexes = [
            models.Index(fields=["lesson", "date"]),
        ]

    def __str__(self):
        return f"{self.lesson_id} staff {self.date}"


class LessonDateStaffMember(models.Model):
    staff = models.ForeignKey(
        LessonDateStaff,
        on_delete=models.CASCADE,
        related_name="members",
    )
    instructor = models.ForeignKey(
        Member,
        on_delete=models.CASCADE,
        related_name="lesson_date_staff_links",
        limit_choices_to={"is_instructor": True},
    )
    position = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["position", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["staff", "instructor"],
                name="unique_lesson_date_staff_member",
            ),
        ]

    def __str__(self):
        return f"{self.staff_id}:{self.instructor_id}"


class ReservationAddon(models.Model):
    """An extra attached to a named reservation price. lesson is empty for the club catalog."""

    club = models.ForeignKey(
        Club,
        on_delete=models.CASCADE,
        related_name="reservation_addons",
    )
    lesson = models.ForeignKey(
        Lesson,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="reservation_addons",
    )
    name = models.CharField(max_length=80)
    amount = models.PositiveIntegerField(default=0)
    position = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["position", "id"]


class ReservationPrice(models.Model):
    """A named base price. lesson is empty when this price belongs to the club catalog."""

    class Audience(models.TextChoices):
        VISITOR = "visitor", "Visitor"
        MEMBER = "member", "Member"
        TRIAL = "trial", "Trial"
        ANY = "any", "Any"

    club = models.ForeignKey(
        Club,
        on_delete=models.CASCADE,
        related_name="reservation_prices",
    )
    lesson = models.ForeignKey(
        Lesson,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="reservation_prices",
    )
    name = models.CharField(max_length=80)
    amount = models.PositiveIntegerField(default=0)
    position = models.PositiveIntegerField(default=0)
    audience = models.CharField(
        max_length=10,
        choices=Audience.choices,
        default=Audience.ANY,
    )
    addons = models.ManyToManyField(
        ReservationAddon,
        blank=True,
        related_name="base_prices",
    )

    class Meta:
        ordering = ["position", "id"]


class LessonPriceOffer(models.Model):
    """A club price this lesson offers. Add-ons follow the club unless overridden."""

    lesson = models.ForeignKey(
        Lesson,
        on_delete=models.CASCADE,
        related_name="price_offers",
    )
    price = models.ForeignKey(
        ReservationPrice,
        on_delete=models.CASCADE,
        related_name="lesson_offers",
    )
    addons_overridden = models.BooleanField(default=False)
    addons = models.ManyToManyField(
        ReservationAddon,
        blank=True,
        related_name="offer_overrides",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["lesson", "price"],
                name="unique_lesson_price_offer",
            )
        ]


class Reservation(models.Model):

    class ReservationType(models.TextChoices):
        TRIAL = "trial", "Trial"
        VISITOR = "visitor", "Visitor"
        MEMBER = "member", "Member"

    class Status(models.TextChoices):
        UNPAID = "unpaid", "Unpaid"
        PAID = "paid", "Paid"
        NOT_STARTED = "not_started", "Not started"

    class PaymentMethod(models.TextChoices):
        STRIPE = "stripe", "Stripe"
        TICKET = "ticket", "Ticket"
        CASH = "cash", "Cash at the venue"

    lesson = models.ForeignKey(
        Lesson,
        on_delete=models.CASCADE,
        related_name="reservations"
    )

    payment_method = models.CharField(
        max_length=20,
        choices=PaymentMethod.choices,
        null=True,
        blank=True,
    )

    club = models.ForeignKey(
        Club,
        on_delete=models.CASCADE,
        related_name="reservations"
    )

    # Optional because trial/visitor reservations don't need
    # to create a Member.
    member = models.ForeignKey(
        Member,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reservations"
    )
    user = models.ForeignKey(CustomUser, null=True, blank=True, on_delete=models.SET_NULL, related_name="reservations")

    reservation_type = models.CharField(
        max_length=20,
        choices=ReservationType.choices
    )
    confirmation_email_sent = models.BooleanField(
        default=False,
    )

    trial_staff_email_sent = models.BooleanField(
        default=False,
    )

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.UNPAID
    )

    full_name = models.CharField(max_length=200)
    email = models.EmailField()

    stripe_checkout_session_id = models.CharField(
        max_length=255,
        null=True,
        blank=True,
    )

    stripe_payment_intent_id = models.CharField(
        max_length=255,
        null=True,
        blank=True,
    )

    # Shared by everyone submitted on one form. The email link
    # looks this up and starts checkout for the whole group.
    start_token = models.CharField(
        max_length=64,
        blank=True,
        default="",
        db_index=True,
    )

    requested_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    checkout_started_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    phone_number = models.CharField(
        max_length=30,
        blank=True
    )

    age = models.PositiveIntegerField(null=True, blank=True)
    gender = models.CharField(
        max_length=10,
        choices=[("male", "Male"), ("female", "Female")],
        blank=True,
    )

    # Actual amount charged for this reservation.
    # This should be stored rather than calculated from Lesson later.
    amount = models.PositiveIntegerField(
        default=0
    )
    # Snapshot of the service the guest picked. Prices can change later.
    price_name = models.CharField(max_length=200, blank=True, default="")
    net_yen = models.IntegerField(
        null=True,
        blank=True,
        help_text="Yen that reached the club after Stripe's fee.",
    )

    currency = models.CharField(
        max_length=10,
        default="jpy"
    )

    created_at = models.DateTimeField(
        auto_now_add=True
    )

    paid_at = models.DateTimeField(
        null=True,
        blank=True
    )

    reservation_date = models.DateField()
    reservation_key = models.CharField(max_length=255, unique=True)
    canceled = models.BooleanField(default=False)
    is_deleted = models.BooleanField(default=False)

    # Pay-at-the-venue reservations. status is "paid" (confirmed) from the
    # moment the booking is made; paid_at stays empty until the owner marks
    # the cash as received. A no-show never counts toward the platform fee.
    no_show = models.BooleanField(default=False)
    platform_fee_applied = models.BooleanField(default=False)

    refunded_at = models.DateTimeField(null=True, blank=True)

    refund_started_at = models.DateTimeField(null=True, blank=True)

    stripe_refund_id = models.CharField(
        max_length=255,
        null=True,
        blank=True,
    )

    class Meta:
        indexes = [
            models.Index(
                fields=["lesson", "status"]
            ),
            models.Index(
                fields=["club", "created_at"]
            ),
        ]

    def __str__(self):
        return (
            f"{self.full_name} - "
            f"{self.lesson.title} - "
            f"{self.reservation_type}"
        )



class TicketType(models.Model):
    club = models.ForeignKey(
        Club,
        on_delete=models.CASCADE,
        related_name="ticket_types",
    )

    name = models.CharField(max_length=100)

    eligible_plans = models.ManyToManyField(
        MembershipPlan,
        blank=True,
        related_name="ticket_types",
    )

    description = models.TextField(blank=True)

    active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name

class TicketPackage(models.Model):
    club = models.ForeignKey(
        Club,
        on_delete=models.CASCADE,
        related_name="ticket_packages",
    )

    ticket_type = models.ForeignKey(
        TicketType,
        on_delete=models.CASCADE,
        related_name="packages",
    )

    name = models.CharField(max_length=100)

    description = models.TextField(blank=True)

    quantity = models.PositiveIntegerField()

    price = models.PositiveIntegerField()

    currency = models.CharField(
        max_length=10,
        default="jpy",
    )

    ticket_expiration_mode = models.CharField(
        max_length=30,
        choices=MembershipPlan.TicketExpirationMode.choices,
        default=MembershipPlan.TicketExpirationMode.ONE_MONTH,
    )

    ticket_expiration_days = models.PositiveIntegerField(
        null=True,
        blank=True,
    )

    stripe_price_id = models.CharField(
        max_length=255,
        blank=True,
        null=True,
    )

    stripe_product_id = models.CharField(
        max_length=255,
        blank=True,
        null=True,
    )

    active = models.BooleanField(default=True)

    created_at = models.DateTimeField(
        auto_now_add=True
    )

    updated_at = models.DateTimeField(
        auto_now=True
    )

    def __str__(self):
        return self.name

class TicketPurchase(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PAID = "paid", "Paid"
        FAILED = "failed", "Failed"
        REFUNDED = "refunded", "Refunded"

    member = models.ForeignKey(
        Member,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ticket_purchases",
    )
    member_name = models.CharField(max_length=200, blank=True, default="")

    package = models.ForeignKey(
        TicketPackage,
        on_delete=models.CASCADE,
        related_name="purchases",
    )

    # Snapshot the purchase information.
    # This protects historical records if the package changes later.
    quantity = models.PositiveIntegerField()
    amount = models.PositiveIntegerField()
    currency = models.CharField(
        max_length=10,
        default="jpy",
    )

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
    )

    stripe_checkout_session_id = models.CharField(
        max_length=255,
        unique=True,
        null=True,
        blank=True,
    )

    stripe_payment_intent_id = models.CharField(
        max_length=255,
        unique=True,
        null=True,
        blank=True,
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    paid_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    def __str__(self):
        name = self.member.full_name if self.member_id else (self.member_name or "削除された会員")
        package = self.package.name if self.package_id else "チケット"
        return f"{name} - {package} - {self.amount}"

class TicketGrant(models.Model):
    class Source(models.TextChoices):
        PURCHASE = "purchase", "Purchase"
        MANUAL = "manual", "Manual"
        SUBSCRIPTION = "subscription", "Subscription"

    member = models.ForeignKey(
        Member,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ticket_grants",
    )
    member_name = models.CharField(max_length=200, blank=True, default="")
    dealt_with = models.BooleanField(default=False)

    mutation = models.ForeignKey(
        SubscriptionMutation,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="ticket_grants",
    )

    ticket_type = models.ForeignKey(
        TicketType,
        on_delete=models.CASCADE,
        related_name="grants",
    )

    source = models.CharField(
        max_length=20,
        choices=Source.choices,
    )

    package = models.ForeignKey(
        TicketPackage,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="grants",
    )

    quantity = models.PositiveIntegerField()

    granted_at = models.DateTimeField(
        auto_now_add=True
    )

    stripe_event_id = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        unique=True,
    )

    expires_at = models.DateTimeField(
        null=True,
        blank=True
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["mutation"],
                name="unique_ticket_grant_per_subscription_mutation",
        ),
    ]

    def __str__(self):
        name = self.member.full_name if self.member_id else (self.member_name or "削除された会員")
        ticket = self.ticket_type.name if self.ticket_type_id else "チケット"
        return f"{name} - {ticket} - {self.quantity}"



class TicketUsage(models.Model):
    grant = models.ForeignKey(
        TicketGrant,
        on_delete=models.CASCADE,
        related_name="usages",
    )

    reservation = models.OneToOneField(
        Reservation,
        on_delete=models.CASCADE,
        related_name="ticket_usage",
    )

    quantity = models.PositiveIntegerField(
        default=1
    )

    used_at = models.DateTimeField(
        auto_now_add=True
    )

    refunded_at = models.DateTimeField(
        null=True,
        blank=True
    )

class Participation(models.Model):
    member = models.ForeignKey('Member', on_delete=models.CASCADE, related_name='participations')
    lesson = models.ForeignKey('Lesson', on_delete=models.SET_NULL, null=True, blank=True, related_name='participations')
    
    total_count = models.PositiveIntegerField(default=0)
    monthly_count = models.PositiveIntegerField(default=0)
    last_participation_date = models.DateField(null=True, blank=True)
    
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
    accepted_join_terms = models.TextField(
        blank=True,
        default="",
        help_text="The join terms accepted with this request. Copied onto the member when accepted.",
    )
    other_information = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    already_subscribed_plans = models.ManyToManyField(
        MembershipPlan,
        blank=True,
        related_name="legacy_join_requests",
    )
    is_worker = models.BooleanField(
        default=False,
        help_text="When true, acceptance creates a worker Member with no membership plan.",
    )

    class Meta:
        unique_together = ("user", "club")
        indexes = [
            models.Index(fields=["club"]),
            models.Index(fields=["created_at"]),
        ]

    def __str__(self):
        return f"{self.full_name} → {self.club.subdomain}"


class LevelSystem(models.Model):
    club = models.ForeignKey(Club, on_delete=models.CASCADE, related_name="level_systems")
    name = models.CharField(max_length=40)
    level_names = models.JSONField(null=True, blank=True)
    level_milestones = models.JSONField(null=True, blank=True)
    lessons = models.ManyToManyField(
        "Lesson",
        blank=True,
        related_name="level_systems",
    )

    class Meta:
        ordering = ["id"]
        unique_together = ("club", "name")

    def __str__(self):
        return f"{self.name} ({self.club.subdomain})"


class MemberLevel(models.Model):
    member = models.ForeignKey(Member, on_delete=models.CASCADE, related_name="levels")
    level_system = models.ForeignKey(LevelSystem, on_delete=models.CASCADE, related_name="member_levels")
    level = models.PositiveIntegerField(default=1)
    level_counts = models.JSONField(default=dict)
    manual_level_counts = models.JSONField(default=dict)

    class Meta:
        unique_together = ("member", "level_system")

    def __str__(self):
        return f"{self.member.full_name} - {self.level_system.name} {self.level}"


class JoinRequestLevel(models.Model):
    join_request = models.ForeignKey(JoinRequest, on_delete=models.CASCADE, related_name="levels")
    level_system = models.ForeignKey(LevelSystem, on_delete=models.CASCADE, related_name="join_request_levels")
    level = models.PositiveIntegerField(default=1)
    manual_level_counts = models.JSONField(default=dict)

    class Meta:
        unique_together = ("join_request", "level_system")

    def __str__(self):
        return f"{self.join_request.full_name} - {self.level_system.name} {self.level}"


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


_hex_color = RegexValidator(
    regex=r"^#[0-9A-Fa-f]{6}$",
    message="カラーは #FFFFFF の形式で指定してください。",
)


class Event(models.Model):
    class Audience(models.TextChoices):
        MEMBERS = "members", "Members"
        VISITORS = "visitors", "Visitors"
        BOTH = "both", "Members and visitors"

    club = models.ForeignKey(
        Club,
        on_delete=models.CASCADE,
        related_name="events",
    )
    section_id = models.IntegerField(db_index=True)
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True, default="")
    terms = models.TextField(
        blank=True,
        default="",
        help_text="Optional terms. A reservation requires agreement when this is set.",
    )
    picture = models.CharField(max_length=1000, blank=True, default="")
    starts_at = models.DateTimeField()

    reservation_limit = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Maximum reservations. Empty means unlimited.",
    )
    audience = models.CharField(
        max_length=20,
        choices=Audience.choices,
        default=Audience.BOTH,
    )

    title_color = models.CharField(
        max_length=7,
        default="#1c1917",
        validators=[_hex_color],
    )
    description_color = models.CharField(
        max_length=7,
        default="#57534e",
        validators=[_hex_color],
    )
    detail_color = models.CharField(
        max_length=7,
        default="#44403c",
        validators=[_hex_color],
    )
    button_color = models.CharField(
        max_length=7,
        default="#1c1917",
        validators=[_hex_color],
    )
    button_text_color = models.CharField(
        max_length=7,
        default="#faf6f1",
        validators=[_hex_color],
    )

    archive_visibility = models.CharField(
        max_length=16,
        null=True,
        blank=True,
        choices=[
            ("owner", "Owner"),
            ("members", "Members"),
            ("public", "Public"),
        ],
    )
    reservations_paused = models.BooleanField(default=False)
    canceled_at = models.DateTimeField(null=True, blank=True)
    cancel_reason = models.TextField(blank=True, default="")
    cancel_visible_until = models.DateTimeField(null=True, blank=True)
    live_section_id = models.PositiveIntegerField(null=True, blank=True)
    # How a priced reservation may be paid. Clubs without a connected
    # Stripe account can only take payment at the venue.
    reservation_payment_options = models.CharField(
        max_length=10,
        choices=RESERVATION_PAYMENT_OPTION_CHOICES,
        default="stripe",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["club", "live_section_id"],
                name="unique_live_event_per_section",
            )
        ]

    def __str__(self):
        return f"{self.club.subdomain} - {self.title}"


class EventPrice(models.Model):
    event = models.ForeignKey(
        Event,
        on_delete=models.CASCADE,
        related_name="prices",
    )
    name = models.CharField(max_length=80)
    amount = models.PositiveIntegerField(
        default=0,
        help_text="Price in yen. 0 means free.",
    )
    position = models.PositiveIntegerField(default=0)
    divisions = models.ManyToManyField(
        "Division",
        blank=True,
        related_name="prices",
    )
    brackets = models.ManyToManyField(
        "Bracket",
        blank=True,
        related_name="prices",
    )

    class Meta:
        ordering = ["position", "id"]

    def __str__(self):
        return f"{self.name} ¥{self.amount}"


class EventReservation(models.Model):
    class ReservationType(models.TextChoices):
        VISITOR = "visitor", "Visitor"
        MEMBER = "member", "Member"

    class Status(models.TextChoices):
        UNPAID = "unpaid", "Unpaid"
        PAID = "paid", "Paid"
        NOT_STARTED = "not_started", "Not started"

    event = models.ForeignKey(
        Event,
        on_delete=models.CASCADE,
        related_name="reservations",
    )
    club = models.ForeignKey(
        Club,
        on_delete=models.CASCADE,
        related_name="event_reservations",
    )
    member = models.ForeignKey(
        "Member",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="event_reservations",
    )
    user = models.ForeignKey(
        CustomUser,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="event_reservations",
    )
    reservation_type = models.CharField(
        max_length=20,
        choices=ReservationType.choices,
    )
    payment_method = models.CharField(
        max_length=20,
        blank=True,
        default="",
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.UNPAID,
    )
    full_name = models.CharField(max_length=200)
    email = models.EmailField()
    phone_number = models.CharField(max_length=30, blank=True, default="")
    amount = models.PositiveIntegerField(default=0)
    net_yen = models.IntegerField(
        null=True,
        blank=True,
        help_text="Yen that reached the club after Stripe's fee.",
    )
    price_name = models.CharField(max_length=80, blank=True, default="")
    currency = models.CharField(max_length=10, default="jpy")

    stripe_checkout_session_id = models.CharField(
        max_length=255,
        null=True,
        blank=True,
    )
    stripe_payment_intent_id = models.CharField(
        max_length=255,
        null=True,
        blank=True,
    )
    start_token = models.CharField(
        max_length=64,
        blank=True,
        default="",
        db_index=True,
    )
    requested_at = models.DateTimeField(null=True, blank=True)
    checkout_started_at = models.DateTimeField(null=True, blank=True)
    confirmation_email_sent = models.BooleanField(default=False)
    paid_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    reservation_key = models.CharField(max_length=255, unique=True)
    canceled = models.BooleanField(default=False)
    is_deleted = models.BooleanField(default=False)
    # Pay-at-the-venue reservations. See Reservation for how these work.
    no_show = models.BooleanField(default=False)
    platform_fee_applied = models.BooleanField(default=False)
    refunded_at = models.DateTimeField(null=True, blank=True)
    refund_started_at = models.DateTimeField(null=True, blank=True)
    stripe_refund_id = models.CharField(
        max_length=255,
        null=True,
        blank=True,
    )

    class Meta:
        indexes = [
            models.Index(fields=["event", "status"]),
            models.Index(fields=["club", "created_at"]),
        ]

    def __str__(self):
        return (
            f"{self.full_name} - {self.event.title} - {self.reservation_type}"
        )


class Test(models.Model):
    event = models.OneToOneField(
        Event,
        on_delete=models.CASCADE,
        related_name="test",
    )
    results_public = models.BooleanField(
        default=False,
        help_text="When off, only the owner sees every test result. Each participant sees their own.",
    )
    reservations_open_until = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Registration closes at this time. Start links stop working.",
    )
    frozen_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Scheduled freeze time. Registration locks once this time is reached. Must be at least one hour after reservations_open_until.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.event.title} test"


class TestResult(models.Model):
    test = models.ForeignKey(
        Test,
        on_delete=models.CASCADE,
        related_name="results",
    )
    reservation = models.OneToOneField(
        EventReservation,
        on_delete=models.CASCADE,
        related_name="test_result",
    )
    score = models.CharField(max_length=80, blank=True, default="")

    def __str__(self):
        return f"{self.reservation.full_name} - {self.score}"


class Competition(models.Model):
    event = models.OneToOneField(
        Event,
        on_delete=models.CASCADE,
        related_name="competition",
    )
    results_public = models.BooleanField(
        default=False,
        help_text="When off, only the owner sees every place. Each participant sees their own.",
    )
    reservations_open_until = models.DateTimeField(null=True, blank=True)
    frozen_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Scheduled freeze time. Registration locks once this time is reached. Must be at least one hour after reservations_open_until.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.event.title} competition"


class Division(models.Model):
    competition = models.ForeignKey(
        Competition,
        on_delete=models.CASCADE,
        related_name="divisions",
    )
    name = models.CharField(max_length=80)
    position = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["position", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["competition", "name"],
                name="unique_division_name",
            )
        ]

    def __str__(self):
        return self.name


class DivisionEntry(models.Model):
    division = models.ForeignKey(
        Division,
        on_delete=models.CASCADE,
        related_name="entries",
    )
    reservation = models.ForeignKey(
        EventReservation,
        on_delete=models.CASCADE,
        related_name="division_entries",
    )
    position = models.PositiveIntegerField(default=0)
    place = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        ordering = ["position", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["division", "reservation"],
                name="unique_division_entry",
            ),
            models.UniqueConstraint(
                fields=["division", "place"],
                name="unique_division_place",
            ),
        ]

    def __str__(self):
        return f"{self.reservation.full_name} - {self.division.name}"


class Placement(models.Model):
    competition = models.ForeignKey(
        Competition,
        on_delete=models.CASCADE,
        related_name="placements",
    )
    reservation = models.OneToOneField(
        EventReservation,
        on_delete=models.CASCADE,
        related_name="placement",
    )
    place = models.PositiveIntegerField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["competition", "place"],
                name="unique_competition_place",
            )
        ]

    def __str__(self):
        return f"{self.reservation.full_name} - {self.place}"


class Duel(models.Model):
    event = models.OneToOneField(
        Event,
        on_delete=models.CASCADE,
        related_name="duel",
    )
    results_public = models.BooleanField(
        default=False,
        help_text="When off, only the owner sees every match. Each participant sees their own match.",
    )
    reservations_open_until = models.DateTimeField(null=True, blank=True)
    frozen_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Scheduled freeze time. Registration locks once this time is reached. Must be at least one hour after reservations_open_until.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.event.title} duel"


class Pairing(models.Model):
    duel = models.ForeignKey(
        Duel,
        on_delete=models.CASCADE,
        related_name="pairings",
    )
    position = models.PositiveIntegerField()
    first = models.ForeignKey(
        EventReservation,
        on_delete=models.CASCADE,
        related_name="pairing_first",
    )
    second = models.ForeignKey(
        EventReservation,
        on_delete=models.CASCADE,
        related_name="pairing_second",
    )
    winner = models.ForeignKey(
        EventReservation,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="pairing_wins",
    )
    drawn = models.BooleanField(default=False)
    note = models.CharField(
        max_length=200,
        blank=True,
        default="",
        help_text="Optional reason the winner won, such as a finish or a no-show.",
    )

    class Meta:
        ordering = ["position", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["duel", "position"],
                name="unique_duel_pairing_position",
            ),
            models.UniqueConstraint(fields=["first"], name="unique_pairing_first"),
            models.UniqueConstraint(fields=["second"], name="unique_pairing_second"),
        ]

    def __str__(self):
        return f"{self.first.full_name} vs {self.second.full_name}"


class Tournament(models.Model):
    event = models.OneToOneField(
        Event,
        on_delete=models.CASCADE,
        related_name="tournament",
    )
    results_public = models.BooleanField(
        default=False,
        help_text="When off, only the owner sees every bracket. A participant sees the bracket they are in.",
    )
    reservations_open_until = models.DateTimeField(null=True, blank=True)
    frozen_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Scheduled freeze time. Registration locks once this time is reached. Must be at least one hour after reservations_open_until.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.event.title} tournament"


class Bracket(models.Model):
    tournament = models.ForeignKey(
        Tournament,
        on_delete=models.CASCADE,
        related_name="brackets",
    )
    name = models.CharField(max_length=80)
    position = models.PositiveIntegerField()

    class Meta:
        ordering = ["position", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["tournament", "position"],
                name="unique_tournament_bracket_position",
            )
        ]

    def __str__(self):
        return self.name


class BracketEntry(models.Model):
    bracket = models.ForeignKey(
        Bracket,
        on_delete=models.CASCADE,
        related_name="entries",
    )
    reservation = models.ForeignKey(
        EventReservation,
        on_delete=models.CASCADE,
        related_name="bracket_entries",
    )
    position = models.PositiveIntegerField()

    class Meta:
        ordering = ["position", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["bracket", "reservation"],
                name="unique_bracket_reservation",
            )
        ]

    def __str__(self):
        return self.reservation.full_name


class TournamentMatch(models.Model):
    bracket = models.ForeignKey(
        Bracket,
        on_delete=models.CASCADE,
        related_name="matches",
    )
    round = models.PositiveIntegerField()
    position = models.PositiveIntegerField()
    first = models.ForeignKey(
        EventReservation,
        on_delete=models.CASCADE,
        related_name="tournament_first",
    )
    second = models.ForeignKey(
        EventReservation,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="tournament_second",
    )
    winner = models.ForeignKey(
        EventReservation,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tournament_wins",
    )
    note = models.CharField(max_length=200, blank=True, default="")

    class Meta:
        ordering = ["round", "position", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["bracket", "round", "position"],
                name="unique_tournament_match_position",
            )
        ]

    def __str__(self):
        other = self.second.full_name if self.second_id else "bye"
        return f"{self.first.full_name} vs {other}"


class TeamBattle(models.Model):
    event = models.OneToOneField(
        Event,
        on_delete=models.CASCADE,
        related_name="team_battle",
    )
    results_public = models.BooleanField(
        default=False,
        help_text="When off, only the owner sees every match. A participant sees the match their team is in.",
    )
    reservations_open_until = models.DateTimeField(null=True, blank=True)
    frozen_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Scheduled freeze time. Registration locks once this time is reached. Must be at least one hour after reservations_open_until.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.event.title} team battle"


class BattleTeam(models.Model):
    battle = models.ForeignKey(
        TeamBattle,
        on_delete=models.CASCADE,
        related_name="teams",
    )
    name = models.CharField(max_length=80)
    position = models.PositiveIntegerField()

    class Meta:
        ordering = ["position", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["battle", "position"],
                name="unique_battle_team_position",
            )
        ]

    def __str__(self):
        return self.name


class BattleMember(models.Model):
    team = models.ForeignKey(
        BattleTeam,
        on_delete=models.CASCADE,
        related_name="members",
    )
    reservation = models.OneToOneField(
        EventReservation,
        on_delete=models.CASCADE,
        related_name="battle_membership",
    )
    position = models.PositiveIntegerField()

    class Meta:
        ordering = ["position", "id"]

    def __str__(self):
        return self.reservation.full_name


class BattleMatch(models.Model):
    battle = models.ForeignKey(
        TeamBattle,
        on_delete=models.CASCADE,
        related_name="matches",
    )
    position = models.PositiveIntegerField()
    first = models.ForeignKey(
        BattleTeam,
        on_delete=models.CASCADE,
        related_name="matches_as_first",
    )
    second = models.ForeignKey(
        BattleTeam,
        on_delete=models.CASCADE,
        related_name="matches_as_second",
    )
    winner = models.ForeignKey(
        BattleTeam,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="matches_won",
    )
    drawn = models.BooleanField(default=False)
    note = models.CharField(max_length=200, blank=True, default="")

    class Meta:
        ordering = ["position", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["battle", "position"],
                name="unique_battle_match_position",
            ),
            models.UniqueConstraint(fields=["first"], name="unique_battle_match_first"),
            models.UniqueConstraint(fields=["second"], name="unique_battle_match_second"),
        ]

    def __str__(self):
        return f"{self.first.name} vs {self.second.name}"


class TeamTournament(models.Model):
    event = models.OneToOneField(
        Event,
        on_delete=models.CASCADE,
        related_name="team_tournament",
    )
    results_public = models.BooleanField(
        default=False,
        help_text="When off, only the owner sees every bracket. A participant sees the bracket their team is in.",
    )
    reservations_open_until = models.DateTimeField(null=True, blank=True)
    frozen_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Scheduled freeze time. Registration locks once this time is reached. Must be at least one hour after reservations_open_until.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.event.title} team tournament"


class TournamentTeam(models.Model):
    tournament = models.ForeignKey(
        TeamTournament,
        on_delete=models.CASCADE,
        related_name="teams",
    )
    name = models.CharField(max_length=80)
    position = models.PositiveIntegerField()

    class Meta:
        ordering = ["position", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["tournament", "position"],
                name="unique_tournament_team_position",
            )
        ]

    def __str__(self):
        return self.name


class TournamentTeamMember(models.Model):
    team = models.ForeignKey(
        TournamentTeam,
        on_delete=models.CASCADE,
        related_name="members",
    )
    reservation = models.OneToOneField(
        EventReservation,
        on_delete=models.CASCADE,
        related_name="tournament_team_membership",
    )
    position = models.PositiveIntegerField()

    class Meta:
        ordering = ["position", "id"]

    def __str__(self):
        return self.reservation.full_name


class TeamBracket(models.Model):
    tournament = models.ForeignKey(
        TeamTournament,
        on_delete=models.CASCADE,
        related_name="brackets",
    )
    name = models.CharField(max_length=80)
    position = models.PositiveIntegerField()

    class Meta:
        ordering = ["position", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["tournament", "position"],
                name="unique_team_bracket_position",
            )
        ]

    def __str__(self):
        return self.name


class TeamBracketEntry(models.Model):
    bracket = models.ForeignKey(
        TeamBracket,
        on_delete=models.CASCADE,
        related_name="entries",
    )
    team = models.OneToOneField(
        TournamentTeam,
        on_delete=models.CASCADE,
        related_name="bracket_entry",
    )
    position = models.PositiveIntegerField()

    class Meta:
        ordering = ["position", "id"]

    def __str__(self):
        return self.team.name


class TeamBracketMatch(models.Model):
    bracket = models.ForeignKey(
        TeamBracket,
        on_delete=models.CASCADE,
        related_name="matches",
    )
    round = models.PositiveIntegerField()
    position = models.PositiveIntegerField()
    first = models.ForeignKey(
        TournamentTeam,
        on_delete=models.CASCADE,
        related_name="bracket_first",
    )
    second = models.ForeignKey(
        TournamentTeam,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="bracket_second",
    )
    winner = models.ForeignKey(
        TournamentTeam,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="bracket_wins",
    )
    note = models.CharField(max_length=200, blank=True, default="")

    class Meta:
        ordering = ["round", "position", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["bracket", "round", "position"],
                name="unique_team_bracket_match_position",
            )
        ]

    def __str__(self):
        other = self.second.name if self.second_id else "bye"
        return f"{self.first.name} vs {other}"


class MemberRecord(models.Model):
    class Kind(models.TextChoices):
        TEST = "test", "Test"
        INDIVIDUAL_COMPETITION = "individual_competition", "Individual competition"
        DUEL = "duel", "Duel"
        TEAM_COMPETITION = "team_competition", "Team competition"
        INDIVIDUAL_TOURNAMENT = "individual_tournament", "Individual tournament"
        TEAM_TOURNAMENT = "team_tournament", "Team tournament"

    club = models.ForeignKey(
        Club,
        on_delete=models.CASCADE,
        related_name="member_records",
    )
    member = models.ForeignKey(
        Member,
        on_delete=models.CASCADE,
        related_name="records",
    )
    event = models.ForeignKey(
        Event,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="member_records",
    )
    reservation = models.ForeignKey(
        EventReservation,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="member_records",
    )
    division = models.ForeignKey(
        Division,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="member_records",
    )
    bracket = models.ForeignKey(
        Bracket,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="member_records",
    )
    kind = models.CharField(
        max_length=32,
        choices=Kind.choices,
        default=Kind.TEST,
    )
    name = models.CharField(max_length=200)
    occurred_on = models.DateField(null=True, blank=True)
    result = models.CharField(max_length=80, blank=True, default="")
    description = models.TextField(blank=True, default="")
    details = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-occurred_on", "-id"]
        indexes = [
            models.Index(fields=["member", "occurred_on"]),
        ]

    def __str__(self):
        return f"{self.member.full_name} - {self.name}"


class ExpenseReceipt(models.Model):
    """A club cost, such as equipment, with the receipt photo on Google Cloud."""

    club = models.ForeignKey(
        Club,
        on_delete=models.CASCADE,
        related_name="expense_receipts",
    )
    image = models.ImageField(
        upload_to=club_expense_receipt_upload_to,
        storage=PrivateMediaStorage(),
    )
    title = models.CharField(max_length=200, blank=True, default="")
    amount = models.PositiveIntegerField(
        null=True,
        blank=False,
        help_text="Required when saving. Older rows may still be empty.",
    )
    description = models.TextField(blank=True, default="")
    purchased_on = models.DateField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-purchased_on", "-id"]
        indexes = [
            models.Index(fields=["club", "purchased_on"]),
        ]

    def __str__(self):
        label = self.title or "経費"
        return f"{self.club_id} {self.purchased_on} {label}"


class ClubMonthlyIncome(models.Model):
    """Stored income snapshot for one club and one calendar month.

    Created the first time that month is opened in 収納歴. A later open
    recalculates when an invoice or reservation newer than calculated_at
    is part of the month. teacher_pay_yen is what this club paid
    instructors during the month. The Kaibaru fee is the owner's bill
    for every club, stored here so a one-club month can show it.
    """

    club = models.ForeignKey(
        Club,
        on_delete=models.CASCADE,
        related_name="monthly_incomes",
    )
    month = models.DateTimeField()
    total_yen = models.IntegerField(default=0)
    platform_fee_yen = models.IntegerField(default=0)
    receipt_total_yen = models.IntegerField(default=0)
    teacher_pay_yen = models.IntegerField(default=0)
    worker_pay_yen = models.IntegerField(default=0)
    calculated_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When these totals were last calculated.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["club", "month"],
                name="unique_club_income_month",
            ),
        ]

    def __str__(self):
        return f"{self.club_id} {self.month}"


class ClubManager(models.Model):
    """What a manager may do for one club.

    The row exists while the member is a manager. A manager cannot be
    given permission over their own pay or their own bills.
    """

    member = models.OneToOneField(
        Member,
        on_delete=models.CASCADE,
        related_name="manager_role",
    )
    members = models.BooleanField(default=False)
    plans = models.BooleanField(default=False)
    lessons = models.BooleanField(default=False)
    site = models.BooleanField(default=False)
    events = models.BooleanField(default=False)
    teacher_pay = models.BooleanField(default=False)
    member_payments = models.BooleanField(default=False)
    expenses = models.BooleanField(default=False)
    income = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.member_id}"


class InstructorPay(models.Model):
    """How one club pays one instructor.

    monthly_amount is a fixed salary. The monthly task writes one
    TeacherInvoice for the previous month. Leave it empty when the owner
    writes the statement by hand from the classes that were taught.
    """

    club = models.ForeignKey(
        Club,
        on_delete=models.CASCADE,
        related_name="instructor_pays",
    )
    instructor = models.ForeignKey(
        Member,
        on_delete=models.CASCADE,
        related_name="instructor_pays",
        limit_choices_to=Q(is_instructor=True) | Q(is_manager=True),
    )
    monthly_amount = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Fixed yen per month. Empty means no automatic statement.",
    )
    active = models.BooleanField(default=True)
    note = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["club", "instructor"],
                name="unique_instructor_pay_per_club",
            ),
        ]

    def clean(self):
        super().clean()
        if self.instructor_id and self.club_id:
            if self.instructor.club_id != self.club_id:
                raise ValidationError(
                    {"instructor": "この指導者はこのクラブの会員ではありません。"}
                )
            if not self.instructor.is_instructor and not self.instructor.is_manager:
                raise ValidationError(
                    {"instructor": "会員を指導者か管理人にしてから給与を設定してください。"}
                )

    def __str__(self):
        return f"{self.club_id} {self.instructor_id}"


class TeacherInvoice(models.Model):
    """What the club owes one instructor.

    amount is typed by the owner, or copied from the fixed monthly salary.
    Lesson lines say which classes were taught and how many times. They do
    not add up to amount.
    """

    club = models.ForeignKey(
        Club,
        on_delete=models.CASCADE,
        related_name="teacher_invoices",
    )
    instructor = models.ForeignKey(
        Member,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="teacher_invoices",
    )
    instructor_name = models.CharField(max_length=200, blank=True, default="")
    instructor_pay = models.ForeignKey(
        InstructorPay,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="invoices",
        help_text="Set by the monthly task. Empty on a hand-written statement.",
    )
    year = models.PositiveIntegerField()
    month = models.PositiveIntegerField()
    amount = models.PositiveIntegerField()
    note = models.TextField(blank=True, default="")
    paid_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Empty until the owner marks this statement paid.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-year", "-month", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["instructor_pay", "year", "month"],
                name="unique_monthly_teacher_invoice",
            ),
        ]

    def clean(self):
        super().clean()
        if self.month < 1 or self.month > 12:
            raise ValidationError({"month": "月は1から12にしてください。"})
        if self.instructor_id and self.club_id and self.instructor.club_id != self.club_id:
            raise ValidationError(
                {"instructor": "この指導者はこのクラブの会員ではありません。"}
            )
        if (
            self.instructor_pay_id
            and self.club_id
            and self.instructor_pay.club_id != self.club_id
        ):
            raise ValidationError(
                {"instructor_pay": "給与設定が別のクラブのものです。"}
            )

    def __str__(self):
        return f"{self.club_id} {self.instructor_id} {self.year}-{self.month}"


class TeacherInvoiceLesson(models.Model):
    """One class on a teacher statement, and how many times it was taught.

    occurrence_date + fee_yen are set when a dated session is applied from
    the instructor lesson fee flow. Older hand-written lines may leave them
    empty and only use times_taught.
    """

    invoice = models.ForeignKey(
        TeacherInvoice,
        on_delete=models.CASCADE,
        related_name="lessons",
    )
    lesson = models.ForeignKey(
        "Lesson",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="teacher_invoice_lines",
    )
    lesson_title = models.CharField(max_length=200, blank=True, default="")
    times_taught = models.PositiveIntegerField()
    occurrence_date = models.DateField(
        null=True,
        blank=True,
        help_text="Calendar date of the taught session, when applied from fees.",
    )
    fee_yen = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Yen for this dated session. Empty on hand-written aggregate lines.",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["invoice", "lesson", "occurrence_date"],
                condition=Q(occurrence_date__isnull=False, lesson__isnull=False),
                name="unique_invoice_lesson_occurrence",
            ),
        ]

    def clean(self):
        super().clean()
        if self.times_taught < 1:
            raise ValidationError({"times_taught": "回数は1以上にしてください。"})
        if self.lesson_id and self.invoice_id and self.lesson.club_id != self.invoice.club_id:
            raise ValidationError({"lesson": "このレッスンは別のクラブのものです。"})

    def save(self, *args, **kwargs):
        if self.lesson_id and not self.lesson_title:
            self.lesson_title = self.lesson.title
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.lesson_title} x{self.times_taught}"


class ServiceCourse(models.Model):
    """A bookable salon course with a fixed duration and price."""

    club = models.ForeignKey(
        Club,
        on_delete=models.CASCADE,
        related_name="service_courses",
    )
    name = models.CharField(max_length=80)
    duration_minutes = models.PositiveIntegerField(
        help_text="How long the course takes, in minutes.",
    )
    amount = models.PositiveIntegerField(default=0)
    description = models.TextField(blank=True, default="")
    position = models.PositiveIntegerField(default=0)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["position", "id"]

    def clean(self):
        super().clean()
        if self.duration_minutes < 1:
            raise ValidationError({"duration_minutes": "所要時間は1分以上にしてください。"})

    def __str__(self):
        return f"{self.club_id} {self.name} ({self.duration_minutes}m)"


class ServiceWindow(models.Model):
    """Store open booking window for interval-based schedules.

    One row is one weekday. Customers may start between earliest_start
    and latest_start. The chosen course must finish by max_finish.
    """

    club = models.ForeignKey(
        Club,
        on_delete=models.CASCADE,
        related_name="service_windows",
    )
    section_id = models.IntegerField(null=True, blank=True, db_index=True)
    weekday = models.PositiveSmallIntegerField(
        default=0,
        help_text="0=Monday … 6=Sunday",
    )
    earliest_start = models.TimeField()
    latest_start = models.TimeField()
    max_finish = models.TimeField()
    position = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["weekday", "position", "earliest_start", "id"]

    def clean(self):
        super().clean()
        if self.weekday is None or self.weekday < 0 or self.weekday > 6:
            raise ValidationError({"weekday": "曜日を選択してください。"})
        if self.earliest_start and self.latest_start and self.earliest_start > self.latest_start:
            raise ValidationError({"latest_start": "開始の最遅時刻は最早時刻以降にしてください。"})
        if self.latest_start and self.max_finish and self.latest_start > self.max_finish:
            raise ValidationError({"max_finish": "終了上限は開始の最遅時刻以降にしてください。"})

    def __str__(self):
        return (
            f"{self.club_id} wd{self.weekday} "
            f"{self.earliest_start}-{self.latest_start}"
            f" (finish {self.max_finish})"
        )


class WorkerProfile(models.Model):
    """Salon-worker settings for one Member flagged is_worker."""

    worker = models.OneToOneField(
        Member,
        on_delete=models.CASCADE,
        related_name="worker_profile",
        limit_choices_to={"is_worker": True},
    )
    monthly_hour_cap = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Max bookable hours per calendar month. Empty means unlimited.",
    )
    hourly_rate = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Yen per hour. Used when no fixed monthly salary is set.",
    )
    enrolled_at = models.DateField(default=timezone.localdate)
    courses = models.ManyToManyField(
        ServiceCourse,
        blank=True,
        related_name="workers",
        through="WorkerCourse",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def clean(self):
        super().clean()
        if self.worker_id and not self.worker.is_worker:
            raise ValidationError({"worker": "会員をワーカーにしてから設定してください。"})

    def __str__(self):
        return f"worker:{self.worker_id}"


class WorkerCourse(models.Model):
    """Which courses a worker can perform."""

    profile = models.ForeignKey(
        WorkerProfile,
        on_delete=models.CASCADE,
        related_name="course_links",
    )
    course = models.ForeignKey(
        ServiceCourse,
        on_delete=models.CASCADE,
        related_name="worker_links",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["profile", "course"],
                name="unique_worker_course",
            ),
        ]

    def clean(self):
        super().clean()
        if (
            self.profile_id
            and self.course_id
            and self.profile.worker.club_id != self.course.club_id
        ):
            raise ValidationError({"course": "コースが別のクラブのものです。"})

    def __str__(self):
        return f"{self.profile_id}:{self.course_id}"


class WorkerAvailability(models.Model):
    """Default weekly hours a worker is available for booking."""

    worker = models.ForeignKey(
        Member,
        on_delete=models.CASCADE,
        related_name="worker_availabilities",
        limit_choices_to={"is_worker": True},
    )
    weekday = models.IntegerField(
        choices=[
            (0, "月曜日"),
            (1, "火曜日"),
            (2, "水曜日"),
            (3, "木曜日"),
            (4, "金曜日"),
            (5, "土曜日"),
            (6, "日曜日"),
        ]
    )
    start_time = models.TimeField()
    end_time = models.TimeField()

    class Meta:
        ordering = ["weekday", "start_time", "id"]

    def clean(self):
        super().clean()
        if self.start_time and self.end_time and self.start_time >= self.end_time:
            raise ValidationError({"end_time": "終了時刻は開始時刻より後にしてください。"})
        if self.worker_id and not self.worker.is_worker:
            raise ValidationError({"worker": "会員をワーカーにしてから設定してください。"})

    def __str__(self):
        return f"{self.worker_id} wd{self.weekday} {self.start_time}-{self.end_time}"


class WorkerTimeOff(models.Model):
    """Yasumi: a full day or a time range a worker is unavailable."""

    worker = models.ForeignKey(
        Member,
        on_delete=models.CASCADE,
        related_name="worker_time_offs",
        limit_choices_to={"is_worker": True},
    )
    date = models.DateField()
    start_time = models.TimeField(
        null=True,
        blank=True,
        help_text="Empty with empty end_time means the whole day off.",
    )
    end_time = models.TimeField(null=True, blank=True)
    note = models.CharField(max_length=200, blank=True, default="")

    class Meta:
        ordering = ["-date", "start_time", "id"]
        indexes = [
            models.Index(fields=["worker", "date"]),
        ]

    def clean(self):
        super().clean()
        if self.worker_id and not self.worker.is_worker:
            raise ValidationError({"worker": "会員をワーカーにしてから設定してください。"})
        if (self.start_time is None) != (self.end_time is None):
            raise ValidationError("休みの開始と終了は両方入れるか、両方空にしてください。")
        if self.start_time and self.end_time and self.start_time >= self.end_time:
            raise ValidationError({"end_time": "終了時刻は開始時刻より後にしてください。"})

    def __str__(self):
        return f"{self.worker_id} {self.date} off"


class WorkerPay(models.Model):
    """How one club pays one worker.

    monthly_amount is a fixed salary. Leave empty to use hourly_rate on
    WorkerProfile, or to write statements by hand.
    """

    club = models.ForeignKey(
        Club,
        on_delete=models.CASCADE,
        related_name="worker_pays",
    )
    worker = models.ForeignKey(
        Member,
        on_delete=models.CASCADE,
        related_name="worker_pays",
        limit_choices_to={"is_worker": True},
    )
    monthly_amount = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Fixed yen per month. Empty means no automatic fixed statement.",
    )
    active = models.BooleanField(default=True)
    note = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["club", "worker"],
                name="unique_worker_pay_per_club",
            ),
        ]

    def clean(self):
        super().clean()
        if self.worker_id and self.club_id:
            if self.worker.club_id != self.club_id:
                raise ValidationError(
                    {"worker": "このワーカーはこのクラブの会員ではありません。"}
                )
            if not self.worker.is_worker:
                raise ValidationError(
                    {"worker": "会員をワーカーにしてから給与を設定してください。"}
                )

    def __str__(self):
        return f"{self.club_id} {self.worker_id}"


class WorkerInvoice(models.Model):
    """What the club owes one worker for a calendar month."""

    club = models.ForeignKey(
        Club,
        on_delete=models.CASCADE,
        related_name="worker_invoices",
    )
    worker = models.ForeignKey(
        Member,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="worker_invoices",
    )
    worker_name = models.CharField(max_length=200, blank=True, default="")
    worker_pay = models.ForeignKey(
        WorkerPay,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="invoices",
    )
    year = models.PositiveIntegerField()
    month = models.PositiveIntegerField()
    amount = models.PositiveIntegerField()
    hours_worked = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        default=0,
        help_text="Hours counted toward this statement.",
    )
    reservation_count = models.PositiveIntegerField(default=0)
    note = models.TextField(blank=True, default="")
    paid_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-year", "-month", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["worker_pay", "year", "month"],
                name="unique_monthly_worker_invoice",
            ),
        ]

    def clean(self):
        super().clean()
        if self.month < 1 or self.month > 12:
            raise ValidationError({"month": "月は1から12にしてください。"})
        if self.worker_id and self.club_id and self.worker.club_id != self.club_id:
            raise ValidationError(
                {"worker": "このワーカーはこのクラブの会員ではありません。"}
            )

    def save(self, *args, **kwargs):
        if self.worker_id and not self.worker_name:
            self.worker_name = self.worker.full_name
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.club_id} {self.worker_id} {self.year}-{self.month}"


class ServiceReservation(models.Model):
    """Interval booking assigned to a worker and a course (no Lesson)."""

    class ReservationType(models.TextChoices):
        TRIAL = "trial", "Trial"
        VISITOR = "visitor", "Visitor"
        MEMBER = "member", "Member"

    class Status(models.TextChoices):
        UNPAID = "unpaid", "Unpaid"
        PAID = "paid", "Paid"
        NOT_STARTED = "not_started", "Not started"

    class PaymentMethod(models.TextChoices):
        STRIPE = "stripe", "Stripe"
        TICKET = "ticket", "Ticket"
        CASH = "cash", "Cash at the venue"

    club = models.ForeignKey(
        Club,
        on_delete=models.CASCADE,
        related_name="service_reservations",
    )
    course = models.ForeignKey(
        ServiceCourse,
        on_delete=models.PROTECT,
        related_name="reservations",
    )
    worker = models.ForeignKey(
        Member,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="service_reservations",
        limit_choices_to={"is_worker": True},
    )
    section_id = models.IntegerField(null=True, blank=True, db_index=True)
    member = models.ForeignKey(
        Member,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="service_bookings",
    )
    user = models.ForeignKey(
        CustomUser,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="service_reservations",
    )
    reservation_type = models.CharField(
        max_length=20,
        choices=ReservationType.choices,
        default=ReservationType.VISITOR,
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.UNPAID,
    )
    payment_method = models.CharField(
        max_length=20,
        choices=PaymentMethod.choices,
        null=True,
        blank=True,
    )
    full_name = models.CharField(max_length=200)
    email = models.EmailField()
    phone_number = models.CharField(max_length=30, blank=True, default="")
    amount = models.PositiveIntegerField(default=0)
    price_name = models.CharField(max_length=200, blank=True, default="")
    net_yen = models.IntegerField(null=True, blank=True)
    currency = models.CharField(max_length=10, default="jpy")
    reservation_date = models.DateField()
    start_time = models.TimeField()
    end_time = models.TimeField()
    reservation_key = models.CharField(max_length=255, unique=True)
    stripe_checkout_session_id = models.CharField(max_length=255, null=True, blank=True)
    stripe_payment_intent_id = models.CharField(max_length=255, null=True, blank=True)
    confirmation_email_sent = models.BooleanField(default=False)
    canceled = models.BooleanField(default=False)
    is_deleted = models.BooleanField(default=False)
    no_show = models.BooleanField(default=False)
    platform_fee_applied = models.BooleanField(default=False)
    paid_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    refunded_at = models.DateTimeField(null=True, blank=True)
    refund_started_at = models.DateTimeField(null=True, blank=True)
    stripe_refund_id = models.CharField(max_length=255, null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["club", "reservation_date"]),
            models.Index(fields=["worker", "reservation_date"]),
            models.Index(fields=["course", "status"]),
        ]

    def __str__(self):
        return (
            f"{self.full_name} - {self.course_id} - "
            f"{self.reservation_date} {self.start_time}"
        )