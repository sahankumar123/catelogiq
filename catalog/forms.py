from django import forms
from django.core.exceptions import ValidationError
from databricks.sql import connect
from django.conf import settings


class LoginForm(forms.Form):
    email = forms.EmailField(label='Email', max_length=254, required=True)
    password = forms.CharField(label='Password ', widget=forms.PasswordInput, required=True)


class SignupForm(forms.Form):
    name = forms.CharField(label='Full Name', max_length=255, required=True)
    phone_number = forms.CharField(label='Phone Number', max_length=15, required=True)
    email = forms.EmailField(label='Email', max_length=254, required=True)
    password = forms.CharField(label='Password', widget=forms.PasswordInput, required=True)
    confirm_password = forms.CharField(label='Confirm Password', widget=forms.PasswordInput, required=True)

    def clean_email(self):
        email = self.cleaned_data['email']
        try:
            with connect(
                server_hostname=settings.DATABRICKS_HOST,
                http_path=settings.DATABRICKS_HTTP_PATH,
                access_token=settings.DATABRICKS_TOKEN
            ) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT COUNT(*) FROM text_log_analytics_catalog.login_credentials.login
                    WHERE LOWER(email) = LOWER(?)
                """, (email,))
                count = cursor.fetchone()[0]
                if count > 0:
                    raise ValidationError("Email already registered.")
        except ValidationError:
            raise
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f"Databricks email check failed: {e}")
            raise ValidationError("Error checking email in Databricks.")
        return email

    def clean(self):
        cleaned_data = super().clean()
        password = cleaned_data.get("password")
        confirm = cleaned_data.get("confirm_password")
        if password and confirm and password != confirm:
            raise ValidationError("Passwords do not match.")
        return cleaned_data
