from django.contrib.auth.models import AbstractUser
from django.db import models





class CustomUser(AbstractUser):
    email = models.EmailField(unique=True, null=True, blank=True)
    is_active = models.BooleanField(default=True)
    stripe_customer_id = models.CharField(max_length=255, null=True, blank=True)



    total_max_scores = models.FloatField(default=0)
    total_japanese_score = models.FloatField(default=0.0)
    total_english_5_score = models.FloatField(default=0.0)
    total_english_6_score = models.FloatField(default=0.0)
    total_jr_1_score = models.FloatField(default=0.0)
    total_jr_2_score = models.FloatField(default=0.0)
    total_jr_3_score = models.FloatField(default=0.0)
    total_phonics_score = models.FloatField(default=0.0)
    total_numbers_score = models.FloatField(default=0.0)
    total_eiken_score = models.FloatField(default=0.0)
    total_4eiken_score = models.FloatField(default=0.0)
    total_eiken3_score = models.FloatField(default=0.0)
    total_eiken_pre2_score = models.FloatField(default=0.0)
    total_eiken2_score = models.FloatField(default=0.0)