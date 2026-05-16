from django.urls import re_path, path, include
from . import views, stripe_views, stripe_webhooks
from . import viewsets
from rest_framework.routers import DefaultRouter
from django.contrib.sitemaps.views import sitemap, index
from .sitemaps import ClubSitemap

sitemaps = {
    'clubs': ClubSitemap,
}

router = DefaultRouter()
router.register(r'members', viewsets.MemberViewSet)
router.register(r'join_requests', viewsets.JoinRequestViewSet)
router.register(r'clubs', viewsets.ClubViewSet)
router.register(r'lessons', viewsets.LessonViewSet)
router.register(r'participations', viewsets.ParticipationViewSet)
router.register(r'slate_images', viewsets.SlateImageViewSet)
router.register(r'membershipplans', viewsets.MembershipPlanViewSet)
router.register(r'discounts', viewsets.DiscountViewSet)
router.register(r'member_pricing_adjustments', viewsets.MemberPricingAdjustmentViewSet)

app_name='kaibaru'
urlpatterns = [
    path('api/', include(router.urls)),
    path("start_google_login/", views.start_google_login, name="start_google_login"),
    path('sitemap.xml', sitemap, {'sitemaps': sitemaps}, name='django.contrib.sitemaps.views.sitemap'),
    path('create_checkout_session/<int:club_id>/', stripe_views.create_checkout_session, name='create_checkout_session'),
    # Platform / Club owner subscription events
    path('stripe_webhook/platform/', stripe_webhooks.stripe_platform_webhook, name='stripe_platform_webhook'),

    # Connected account / Member → Club payments
    path('stripe_webhook/connected/', stripe_webhooks.stripe_connected_webhook, name='stripe_connected_webhook'),
    path('unsubscribe/<int:club_id>/', stripe_views.unsubscribe, name='unsubscribe'),
    path('resume_club_subscription/<int:club_id>/', stripe_views.resume_club_subscription, name='resume_club_subscription'),
    path('cancel_member_subscription/<int:item_id>/', stripe_views.cancel_member_subscription, name='cancel_member_subscription'),
    path('resume_member_subscription/<int:item_id>/', stripe_views.resume_member_subscription, name='resume_member_subscription'),
    path('create_stripe_account_link/<int:club_id>/', stripe_views.create_stripe_account_link, name='create_stripe_account_link'),
    path('create_member_checkout_session/<int:club_id>/<int:plan_id>/', stripe_views.create_member_checkout_session, name='create_member_checkout_session'),

    path('update_club_billing_settings/<int:club_id>/', views.update_club_billing_settings, name='update_club_billing_settings'),

    path('stripe_oauth_callback/', stripe_views.stripe_oauth_callback, name='stripe_oauth_callback'),
    path("api/join-request/<str:club_subdomain>/", views.create_join_request, name="create_join_request"),
]