from django.contrib import admin
from django.urls import path, include
from catalog import views

urlpatterns = [
    path('', views.home, name='home'),
    path('login/', views.login_view, name='login'),
    path('signup/', views.signup_view, name='signup'),
    path('logout/', views.logout_view, name='logout'),
    path('verify-otp/', views.verify_otp, name='verify_otp'),
    path('forgot-password/', views.forgot_password, name='forgot_password'),
    path('reset-password/<str:token>/', views.reset_password, name='reset_password'),
    path('log-analytics/', views.log_analytics, name='log_analytics'),
    path('analysis-options/', views.analysis_options, name='analysis_options'),
    path('anomaly-detection/', views.anomaly_detection, name='anomaly_detection'),
    path('stream-viewer/', views.stream_viewer, name='stream_viewer'),
    path('chatbot/', views.chatbot, name='chatbot'),
    path('dashboard/', views.databricks_dashboard_proxy, name='databricks_dashboard'),
    path('about-us/', views.about_us, name='about_us'),
    path('contact/', views.contact, name='contact'),
    path('text-classification/', views.text_classification, name='text_classification'),
    path('debug_session/', views.debug_session, name='debug_session'),
    path('check_pipeline_status/', views.check_pipeline_status, name='check_pipeline_status'),
    path('reset_pipeline_status/', views.reset_pipeline_status, name='reset_pipeline_status'),
    path('api/code-plain/<str:feature_name>/', views.get_feature_code_plain, name='get_feature_code_plain'),
    path('forecast/', views.forecast, name='forecast'),
]
