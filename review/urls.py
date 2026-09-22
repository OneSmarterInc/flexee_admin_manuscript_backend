from django.urls import path
from . import views
from .api_summary import admin_submission_api_summary

urlpatterns = [
    path('health/', views.health),
    path('submissions/', views.submit),
    path('admin/verify-password/', views.admin_verify_password),
    path('admin/login/', views.admin_login),
    path('admin/logout/', views.admin_logout),
    path('admin/session/', views.admin_session),
    path('admin/submissions/', views.admin_submissions),
    path('admin/submissions/<uuid:submission_id>/', views.admin_submission_detail),
    path('admin/submissions/<uuid:submission_id>/accept/', views.admin_submission_accept),
    path('admin/submissions/<uuid:submission_id>/reject/', views.admin_submission_reject),
    path('admin/submissions/<uuid:submission_id>/delete/', views.admin_submission_delete),
    path('admin/submissions/<uuid:submission_id>/send-email/', views.admin_submission_send_email),
    path('admin/submissions/<uuid:submission_id>/api-summary/', admin_submission_api_summary),
    path('admin/submissions/<uuid:submission_id>/download/', views.admin_submission_download),
    path('admin/smtp/', views.admin_smtp_settings),
    path('admin/smtp/test/', views.admin_smtp_test),
]
