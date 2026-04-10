from django.urls import path, re_path
from . import views

urlpatterns = [
    re_path(r'^(?!admin/|accounts/|account/|api/|oauth/|static/|site/).*$', 
            views.KaibaruPageView.as_view(), 
            name='kaibaru_spa'),
]