from django.urls import path
from django.contrib.auth import views as auth_views
from . import views


app_name = "accounts"
urlpatterns = [
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path(
        "update/password/<int:pk>",
        views.StudentUpdateView.as_view(),
        name="update_profile",
    ),
    path("saas/login/", views.SaaSLoginView.as_view(), name="saas-login"),
    path("saas/signup/", views.SaaSSignUpView.as_view(), name="saas-signup"),
    path(
        "verify/<uidb64>/<token>/",
        views.VerifyEmailView.as_view(),
        name="verify_email",
    ),
    path(
        "saas/password-reset/",
        views.PasswordResetRequestView.as_view(),
        name="saas-password-reset",
    ),
    path(
        "password/reset/<uidb64>/<token>/",
        views.PasswordResetConfirmView.as_view(),
        name="password-reset-confirm",
    ),
    path(
        "domain-session/<str:token>/",
        views.DomainSessionHandoffView.as_view(),
        name="domain-session",
    ),
]
