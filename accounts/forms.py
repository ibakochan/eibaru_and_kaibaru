from django import forms
from django.contrib.auth.forms import UserCreationForm
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


class SaaSSignUpForm(forms.ModelForm):
    password = forms.CharField(
        widget=forms.PasswordInput(attrs={'class': 'form-control'})
    )

    class Meta:
        model = CustomUser
        fields = ("username", "email", "password")

    def clean_email(self):
        email = self.cleaned_data["email"].lower()

        if CustomUser.objects.filter(email=email).exists():
            raise forms.ValidationError("このメールアドレスは既に登録されています。")

        return email

    def save(self, commit=True):
        user = super().save(commit=False)
        user.email = self.cleaned_data["email"].lower()
        user.set_password(self.cleaned_data["password"])
        user.is_active = False  # critical
        if commit:
            user.save()
        return user