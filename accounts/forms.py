import re

from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.validators import UnicodeUsernameValidator
from django.core.exceptions import ValidationError
from .models import CustomUser
from main.models import Teacher, Student, Classroom
from django.contrib.auth.forms import AuthenticationForm
from django.utils.crypto import get_random_string

class CustomAuthenticationForm(AuthenticationForm):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['username'].widget.attrs['placeholder'] = 'ユーザネーム'
        self.fields['username'].label = 'ユーザネーム'
        self.fields['password'].widget.attrs['placeholder'] = 'パスワード'
        self.fields['password'].label = 'パスワード'

class SignUpForm(forms.ModelForm):
    username = forms.CharField(
        label="ユーザー名",
        required=True,
        widget=forms.TextInput(attrs={'class': 'form-control'}),
    )

    password = forms.CharField(
        label="パスワード",
        required=True,
        widget=forms.TextInput(attrs={'class': 'form-control'}),
    )

    class Meta:
        model = CustomUser
        fields = ('username', 'password')

    def save(self, commit=True):
        user = super().save(commit=False)

        password = self.cleaned_data.get('password')
        user.set_password(password)

        if not user.email:
            user.email = None

        if commit:
            user.save()
        return user

    def clean(self):
        cleaned_data = super().clean()
        return cleaned_data

class TeacherSignUpForm(forms.ModelForm):
    username = forms.CharField(
        label="ユーザー名",
        required=True,
        widget=forms.TextInput(attrs={'class': 'form-control'}),
    )
    
    last_name = forms.CharField(
        label="名字",
        required=True,
        widget=forms.TextInput(attrs={'class': 'form-control'}),
    )

    email = forms.CharField(
        label="メール",
        required=False,
        widget=forms.TextInput(attrs={'class': 'form-control'}),
    )

    class Meta:
        model = CustomUser
        fields = ('username', 'password', 'last_name', 'email')

    def save(self, commit=True):
        user = super().save(commit=False)
        if commit:
            user.save()
        return user

    def clean(self):
        cleaned_data = super().clean()
        return cleaned_data


class StudentSignUpForm(forms.ModelForm):
    username = forms.CharField(
        label="ユーザー名",
        required=True,
        widget=forms.TextInput(attrs={'class': 'form-control'}),
    )

    last_name = forms.CharField(
        label="名字",
        required=False,
        widget=forms.TextInput(attrs={'class': 'form-control'}),
    )

    password = forms.CharField(
        label="パスワード",
        required=True,
        widget=forms.TextInput(attrs={'class': 'form-control'}),
    )

    student_number = forms.CharField(
        label="出席番号",
        required=False,
        widget=forms.TextInput(attrs={'class': 'form-control'}),
    )

    class Meta:
        model = CustomUser
        fields = ('username', 'password', 'last_name')

    def save(self, commit=True):
        user = super().save(commit=False)
        password = self.cleaned_data.get('password')
        user.set_password(password)
        if commit:
            user.save()
            student_number = 0
            student_number = self.cleaned_data.get('student_number')
            student = Student.objects.create(user=user, student_number=student_number)
        return user

    def clean(self):
        cleaned_data = super().clean()
        return cleaned_data


def username_from_email(email):
    """A unique username for an owner who signs up with an email only."""
    candidate = (email or "").strip().lower()[:150]
    validator = UnicodeUsernameValidator()
    try:
        validator(candidate)
    except ValidationError:
        local = candidate.split("@", 1)[0]
        slug = re.sub(r"[^\w.@+-]", "", local) or "owner"
        candidate = slug[:150]
    username = candidate
    suffix = 2
    while CustomUser.objects.filter(username=username).exists():
        extra = f"_{suffix}"
        username = f"{candidate[:150 - len(extra)]}{extra}"
        suffix += 1
    return username


class SaaSSignUpForm(forms.ModelForm):
    username = forms.CharField(required=False, max_length=150)
    password = forms.CharField(
        widget=forms.PasswordInput(attrs={'class': 'form-control'})
    )

    def __init__(self, *args, english=False, **kwargs):
        self.english = english
        super().__init__(*args, **kwargs)

    class Meta:
        model = CustomUser
        fields = ("username", "email", "password")

    def clean_email(self):
        email = self.cleaned_data["email"].lower()

        if CustomUser.objects.filter(email=email).exists():
            raise forms.ValidationError(
                "That email is already registered."
                if self.english
                else "このメールアドレスは既に登録されています。"
            )

        return email

    def clean_password(self):
        password = self.cleaned_data.get("password") or ""
        if len(password) < 8:
            raise forms.ValidationError(
                "Use at least 8 characters."
                if self.english
                else "パスワードは8文字以上にしてください。"
            )
        return password

    def clean_username(self):
        return (self.cleaned_data.get("username") or "").strip()

    def clean(self):
        cleaned = super().clean()
        email = cleaned.get("email")
        if email and not cleaned.get("username"):
            cleaned["username"] = username_from_email(email)
        return cleaned

    def save(self, commit=True):
        user = super().save(commit=False)
        user.email = self.cleaned_data["email"].lower()
        user.username = self.cleaned_data["username"]
        user.set_password(self.cleaned_data["password"])
        user.is_active = False  # critical
        if commit:
            user.save()
        return user