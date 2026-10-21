from django.contrib import admin
from .models import (
    Member,
    Club,
    ClubMonthlyIncome,
    Lesson,
    Event,
    EventPrice,
    EventReservation,
    Test,
    TestResult,
    Competition,
    Division,
    DivisionEntry,
    Placement,
    Duel,
    Pairing,
    Tournament,
    Bracket,
    BracketEntry,
    TournamentMatch,
    TeamBattle,
    BattleTeam,
    BattleMember,
    BattleMatch,
    TeamTournament,
    TournamentTeam,
    TournamentTeamMember,
    TeamBracket,
    TeamBracketEntry,
    TeamBracketMatch,
    MemberRecord,
    ExpenseReceipt,
    Participation,
    SlateImage,
    JoinRequest,
    MembershipPlan,
    OneTimeProduct,
    OneTimePayment,
    Subscription,
    SubscriptionItem,
    PlatformSubscription,
    PlatformClub,
    ClubSubscriptionMutation,
    Invoice,
    InvoiceItem,
    Payment,
    PaymentMethod,
    StripeCustomer,
    Discount,
    DiscountCondition,
    MemberPricingAdjustment,
    SubscriptionMutation,
    Reservation,
    TicketType,
    TicketPackage,
    TicketPurchase,
    TicketGrant,
    TicketUsage,
)

admin.site.register([
    Club,
    Member,
    Lesson,
    Event,
    EventPrice,
    EventReservation,
    Test,
    TestResult,
    Competition,
    Division,
    DivisionEntry,
    Placement,
    Duel,
    Pairing,
    Tournament,
    Bracket,
    BracketEntry,
    TournamentMatch,
    TeamBattle,
    BattleTeam,
    BattleMember,
    BattleMatch,
    TeamTournament,
    TournamentTeam,
    TournamentTeamMember,
    TeamBracket,
    TeamBracketEntry,
    TeamBracketMatch,
    MemberRecord,
    ExpenseReceipt,
    Participation,
    SlateImage,
    JoinRequest,
    MembershipPlan,
    OneTimeProduct,
    OneTimePayment,
    Subscription,
    SubscriptionItem,
    Invoice,
    InvoiceItem,
    Payment,
    PaymentMethod,
    StripeCustomer,
    Discount,
    DiscountCondition,
    MemberPricingAdjustment,
    SubscriptionMutation,
    Reservation,
    TicketType,
    TicketPackage,
    TicketPurchase,
    TicketGrant,
    TicketUsage,
])


class PlatformSubscriptionAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "user",
        "status",
        "stripe_subscription_id",
        "stripe_customer_id",
        "current_period_end",
        "cancel_at_period_end",
        "last_paid_invoice_id",
    )
    list_filter = ("status", "cancel_at_period_end")
    search_fields = (
        "stripe_subscription_id",
        "stripe_customer_id",
        "last_paid_invoice_id",
        "user__email",
    )
    raw_id_fields = ("user",)
    readonly_fields = ("created_at", "updated_at")


class PlatformClubAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "club",
        "platform_subscription",
        "awaiting_checkout",
        "pending_removal",
        "removal_kind",
        "access_until",
    )
    list_filter = ("awaiting_checkout", "pending_removal", "removal_kind")
    search_fields = ("club__subdomain", "club__title")
    raw_id_fields = ("platform_subscription", "club")
    readonly_fields = ("created_at",)


class ClubSubscriptionMutationAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "platform_subscription",
        "club",
        "type",
        "status",
        "local_state_applied",
        "stripe_finished",
        "created_at",
        "processed_at",
    )
    list_filter = ("type", "status", "local_state_applied", "stripe_finished")
    search_fields = ("club__subdomain", "platform_subscription__stripe_subscription_id")
    raw_id_fields = ("platform_subscription", "club")
    readonly_fields = ("created_at",)


class ClubMonthlyIncomeAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "club",
        "month",
        "total_yen",
        "platform_fee_yen",
        "receipt_total_yen",
        "created_at",
    )
    search_fields = ("club__subdomain", "club__title")
    raw_id_fields = ("club",)
    readonly_fields = ("created_at",)
    date_hierarchy = "month"


admin.site.register(PlatformSubscription, PlatformSubscriptionAdmin)
admin.site.register(PlatformClub, PlatformClubAdmin)
admin.site.register(ClubSubscriptionMutation, ClubSubscriptionMutationAdmin)
admin.site.register(ClubMonthlyIncome, ClubMonthlyIncomeAdmin)