from django.urls import path, include
from django.contrib.auth import views as auth_views
from . import views
from django.urls import reverse_lazy
from mozilla_django_oidc.views import OIDCAuthenticationRequestView, OIDCAuthenticationCallbackView


app_name='accounts'
urlpatterns = [
    path('logout/', auth_views.LogoutView.as_view(), name='logout'),

    path('login/', views.CustomLoginView.as_view(template_name='accounts/login.html'), name='login'),
    path('login/ar/', views.CustomLoginView.as_view(template_name='accounts/login.html'), name='login_ar'),
    path('signup/', views.SignUpView.as_view(template_name='accounts/signup.html'), name='signup'),
    path('signup/ar/', views.SignUpView.as_view(template_name='accounts/signup.html'), name='signup_ar'),
    path('update/password/<int:pk>', views.StudentUpdateView.as_view(), name='update_profile'),
    path("saas/login/", views.SaaSLoginView.as_view(), name="saas-login"),
    path("saas/signup/", views.SaaSSignUpView.as_view(), name="saas-signup"),
    path(
        "verify/<uidb64>/<token>/",
        views.VerifyEmailView.as_view(),
        name="verify_email",
    ),
]