from django.db import models
from django.utils import timezone


class PasswordResetToken(models.Model):
    email = models.EmailField(max_length=254)
    token = models.CharField(max_length=36, unique=True)  # UUID length
    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()

    def __str__(self):
        return f"Token for {self.email}"

    def is_valid(self):
        return timezone.now() <= self.expires_at
