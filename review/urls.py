from django.urls import path
from . import views
from . import author_api
from .api_summary import admin_submission_api_summary

urlpatterns = [
    path('health/', views.health),
    path('submissions/', views.submit),

    # Author scholarly-network workflow.
    path('author/manuscripts/', author_api.author_manuscripts),
    path('author/manuscripts/<uuid:manuscript_id>/', author_api.author_manuscript_detail),
    path('author/manuscripts/<uuid:manuscript_id>/readiness/', author_api.author_readiness),
    path('author/manuscripts/<uuid:manuscript_id>/readiness/run/', author_api.author_run_readiness),
    path('author/manuscripts/<uuid:manuscript_id>/matches/', author_api.author_matches),
    path('author/manuscripts/<uuid:manuscript_id>/matches/run/', author_api.author_generate_matches),
    path('author/manuscripts/<uuid:manuscript_id>/submissions/', author_api.author_create_submission),
    path('author/venues/', author_api.public_venues),
    path('author/venue-submissions/<uuid:submission_id>/', author_api.author_submission_detail),
    path('author/venue-submissions/<uuid:submission_id>/submit/', author_api.author_submit_packet),
    path('author/venue-submissions/<uuid:submission_id>/transfer/', author_api.author_transfer_submission),

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

    # Subscriber / venue configuration.
    path('admin/venues/', author_api.admin_venues),
    path('admin/venues/<uuid:venue_id>/config/', author_api.admin_venue_config),
    path('admin/venues/<uuid:venue_id>/feedback/', author_api.admin_editor_feedback),

    path('admin/smtp/', views.admin_smtp_settings),
    path('admin/smtp/test/', views.admin_smtp_test),
]
