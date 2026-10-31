import json
from django.http import JsonResponse
from django.views import View
from django.shortcuts import get_object_or_404, render, redirect
from .models import CustomUser
from main.models import Student, Teacher, Classroom
from django.contrib.auth import authenticate, login
from allauth.account.views import LoginView
from .forms import CustomAuthenticationForm, SignUpForm, SaaSSignUpForm
from django.views.decorators.cache import never_cache
from django.utils.decorators import method_decorator
from django.contrib.auth.forms import AuthenticationForm
from django.views.decorators.cache import never_cache
from django.shortcuts import redirect
from django.conf import settings
import requests

from django.utils.http import urlsafe_base64_decode
from django.utils.encoding import force_str
from django.http import HttpResponse
from .tokens import email_verification_token, password_reset_token
from .tasks_emails import send_password_reset_email, send_verification_email

import logging
logger = logging.getLogger(__name__)




class SaaSLoginView(View):
    def post(self, request):
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse(
                {"detail": "Invalid JSON"},
                status=400
            )

        email = data.get("email")
        password = data.get("password")

        if not email or not password:
            return JsonResponse(
                {"detail": "Email and password required"},
                status=400
            )

        email = email.lower()

        try:
            user = CustomUser.objects.get(email=email)
        except CustomUser.DoesNotExist:
            return JsonResponse(
                {"detail": "Invalid credentials"},
                status=401
            )

        if not user.is_active:
            return JsonResponse(
                {"detail": "Please verify your email before logging in."},
                status=403
            )

        user = authenticate(request, username=user.username, password=password)

        if user is None:
            return JsonResponse(
                {"detail": "Invalid credentials"},
                status=401
            )

        login(request, user)

        return JsonResponse({"detail": "Login successful"})

@method_decorator(never_cache, name='dispatch')
class CustomLoginView(View):
    template_name = 'accounts/login.html'

    def get(self, request, *args, **kwargs):
        form = AuthenticationForm(request)
        return render(request, self.template_name, {'form': form, 'path': request.path})

    def post(self, request, *args, **kwargs):
        form = AuthenticationForm(request, data=request.POST)

        if form.is_valid():
            username = form.cleaned_data.get('username')
            password = form.cleaned_data.get('password')

            user = authenticate(request, username=username, password=password)

            if user is not None:
                login(request, user)
                return redirect("/")
            else:
                form.add_error(None, "ユーザーネームまたはパスワードが間違っています。")
                return render(request, self.template_name, {'form': form, 'path': request.path})

        return render(request, self.template_name, {'form': form, 'path': request.path})




@method_decorator(never_cache, name='dispatch')
class SignUpView(View):
    template_name = 'accounts/signup.html'

    def get(self, request):
        form = SignUpForm()
        return render(request, self.template_name, {'form': form, 'path': request.path})

    def post(self, request):
        form = SignUpForm(request.POST)

        username = request.POST.get('username')
        password = request.POST.get('password')
        if CustomUser.objects.filter(username=username).exists():
            if '/ar/' in request.path:
                error_message = "اسم المستخدم هذا مستخدم بالفعل"
            else:
                error_message = "このユーザネームはすでに使われている"
            return render(request, self.template_name, {'form': form, 'error_message': error_message, 'path': request.path})

        if len(username) > 10:
            if '/ar/' in request.path:
                error_message = "اسم المستخدم يجب أن لا يتجاوز 10 أحرف"
            else:
                error_message = "ユーザーネームは最大１０文字"
            return render(request, self.template_name, {'form': form, 'error_message': error_message, 'path': request.path})


        if form.is_valid():
            user = form.save()
            user = authenticate(request, username=username, password=password)
            login(request, user)
            return redirect("/")
        else:
            if '/ar/' in request.path:
                error_message = "لا يمكن أن يحتوي اسم المستخدم على مسافات أو رموز"
            else:
                error_message = "ユーザーネームにスペースや記号などは入れません"
            return render(request, self.template_name, {'form': form, 'error_message': error_message, 'path': request.path})
        return render(request, self.template_name, {'form': form, 'path': request.path})

class StudentUpdateView(View):
    def post(self, request, pk):
        user = get_object_or_404(CustomUser, pk=pk)
        student = get_object_or_404(Student, user=user)
        teacher = get_object_or_404(Teacher, user=request.user)

        shared_classroom_exists = Classroom.objects.filter(
            teacher=teacher,
            students=student
        ).exists()

        if not shared_classroom_exists:
            return HttpResponseForbidden("You don't have permission to reset this student's password.")

        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({'error': 'Invalid JSON'}, status=400)

        password = data.get('password')
        if not password:
            return JsonResponse({'error': 'Password is required'}, status=400)

        user.set_password(password)
        user.save()

        return JsonResponse({'message': 'Password reset successful'})



class SaaSSignUpView(View):
    def post(self, request):
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse(
                {"detail": "Invalid JSON"},
                status=400
            )

        form = SaaSSignUpForm(data)

        if form.is_valid():
            user = form.save()
            send_verification_email.delay(user.id)

            return JsonResponse(
                {"detail": "Verification email sent."},
                status=201
            )

        return JsonResponse(form.errors, status=400)

class VerifyEmailView(View):
    def get(self, request, uidb64, token):
        try:
            uid = force_str(urlsafe_base64_decode(uidb64))
            user = CustomUser.objects.get(pk=uid)
        except Exception:
            user = None

        if not user or not email_verification_token.check_token(user, token):
            return redirect("https://kaibaru.jp/verification-failed")

        user.is_active = True
        user.save()

        login(
            request,
            user,
            backend="allauth.account.auth_backends.AuthenticationBackend"
        )

        return redirect("https://kaibaru.jp/?verified=true")


def _user_from_uid(uidb64):
    try:
        uid = force_str(urlsafe_base64_decode(uidb64))
        return CustomUser.objects.get(pk=uid)
    except Exception:
        return None


class PasswordResetRequestView(View):
    """Email a password form when the address belongs to a regular account."""

    def post(self, request):
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({"detail": "無効なリクエストです。"}, status=400)

        email = (data.get("email") or "").strip().lower()
        if not email or "@" not in email:
            return JsonResponse(
                {"detail": "メールアドレスを入力してください。"},
                status=400,
            )

        user = CustomUser.objects.filter(email__iexact=email).first()
        if user and user.has_usable_password() and user.email:
            send_password_reset_email.delay(user.id)

        return JsonResponse({
            "detail": (
                "ご入力のメールアドレスにアカウントがある場合、"
                "パスワード再設定のご案内を送信しました。"
            ),
        })


class PasswordResetConfirmView(View):
    template_name = "accounts/password_reset_confirm.html"

    def get(self, request, uidb64, token):
        user = _user_from_uid(uidb64)
        valid = bool(user and password_reset_token.check_token(user, token))
        return render(request, self.template_name, {
            "valid": valid,
            "done": False,
        })

    def post(self, request, uidb64, token):
        user = _user_from_uid(uidb64)
        if not user or not password_reset_token.check_token(user, token):
            return render(request, self.template_name, {
                "valid": False,
                "done": False,
            })

        password = request.POST.get("password") or ""
        confirm = request.POST.get("confirm") or ""
        error = None
        if len(password) < 8:
            error = "パスワードは8文字以上にしてください。"
        elif password != confirm:
            error = "確認用のパスワードが一致しません。"

        if error:
            return render(request, self.template_name, {
                "valid": True,
                "done": False,
                "error": error,
            })

        user.set_password(password)
        user.is_active = True
        user.save(update_fields=["password", "is_active"])
        return render(request, self.template_name, {
            "valid": True,
            "done": True,
        })


class DomainSessionHandoffView(View):
    def get(self, request, token):
        from django.core.cache import cache
        from kaibaru.custom_domain_hosts import handoff_cache_key

        payload = cache.get(handoff_cache_key(token))
        cache.delete(handoff_cache_key(token))
        if isinstance(payload, dict):
            user_id = payload.get("user_id")
            path = payload.get("path") or "/"
        else:
            user_id = payload
            path = "/"
        if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
            path = "/"
        user = CustomUser.objects.filter(pk=user_id, is_active=True).first() if user_id else None
        if not user:
            return redirect(path)
        login(request, user, backend="allauth.account.auth_backends.AuthenticationBackend")
        return redirect(path)