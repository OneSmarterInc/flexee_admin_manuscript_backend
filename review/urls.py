from django.urls import path
from . import views
from . import author_api
from . import editor_api
from .api_summary import admin_submission_api_summary

urlpatterns = [
    path('health/', views.health),
    path('submissions/', views.submit),
    path('submissions/<uuid:submission_id>/status/', views.submission_status),

    # Author scholarly-network workflow.
    path('author/register/', author_api.author_register),
    path('author/verify-email/', author_api.author_verify_email),
    path('author/login/', author_api.author_login),
    path('author/logout/', author_api.author_logout),
    path('author/session/', author_api.author_session),
    path('author/jobs/<int:job_id>/', author_api.author_job_status),
    path('author/manuscripts/list/', author_api.author_manuscripts_list),
    path('author/manuscripts/', author_api.author_manuscripts),
    path('author/manuscripts/<uuid:manuscript_id>/', author_api.author_manuscript_detail),
    path('author/manuscripts/<uuid:manuscript_id>/readiness/', author_api.author_readiness),
    path('author/manuscripts/<uuid:manuscript_id>/readiness/run/', author_api.author_run_readiness),
    path('author/manuscripts/<uuid:manuscript_id>/readiness/semantic/', author_api.author_run_semantic_readiness),
    path('author/manuscripts/<uuid:manuscript_id>/matches/', author_api.author_matches),
    path('author/manuscripts/<uuid:manuscript_id>/matches/run/', author_api.author_generate_matches),
    path('author/manuscripts/<uuid:manuscript_id>/matches/semantic/', author_api.author_run_semantic_matches),
    path('author/manuscripts/<uuid:manuscript_id>/submissions/', author_api.author_create_submission),
    path('author/venues/', author_api.public_venues),
    path('author/venue-submissions/<uuid:submission_id>/', author_api.author_submission_detail),
    path('author/venue-submissions/<uuid:submission_id>/assessment/run/', author_api.author_run_venue_assessment),
    path('author/venue-submissions/<uuid:submission_id>/submit/', author_api.author_submit_packet),
    path('author/venue-submissions/<uuid:submission_id>/transfer/', author_api.author_transfer_submission),

    path('admin/verify-password/', views.admin_verify_password),
    path('admin/login/', views.admin_login),
    path('admin/logout/', views.admin_logout),
    path('admin/session/', views.admin_session),
    path('admin/submissions/', views.admin_submissions),
    path('admin/queue-health/', views.admin_queue_health),
    path('admin/submissions/<uuid:submission_id>/', views.admin_submission_detail),
    path('admin/submissions/<uuid:submission_id>/accept/', views.admin_submission_accept),
    path('admin/submissions/<uuid:submission_id>/reject/', views.admin_submission_reject),
    path('admin/submissions/<uuid:submission_id>/delete/', views.admin_submission_delete),
    path('admin/submissions/<uuid:submission_id>/send-email/', views.admin_submission_send_email),
    path('admin/submissions/<uuid:submission_id>/api-summary/', admin_submission_api_summary),
    path('admin/submissions/<uuid:submission_id>/download/', views.admin_submission_download),

    # Subscriber / venue configuration.
    path('admin/venues/', author_api.admin_venues),
    path('admin/venues/<uuid:venue_id>/', editor_api.admin_venue_detail),
    path('admin/venues/<uuid:venue_id>/config/', author_api.admin_venue_config),
    path('admin/venues/<uuid:venue_id>/configs/', editor_api.admin_venue_configs),
    path('admin/venues/<uuid:venue_id>/configs/<int:config_id>/activate/', editor_api.admin_activate_venue_config),
    path('admin/venues/<uuid:venue_id>/feedback/', author_api.admin_editor_feedback),

    # Venue editor workspace.
    path('admin/venue-submissions/', editor_api.admin_venue_submissions),
    path('admin/venue-submissions/<uuid:submission_id>/', editor_api.admin_venue_submission_detail),
    path('admin/venue-submissions/<uuid:submission_id>/start-review/', editor_api.admin_start_venue_review),
    path('admin/venue-submissions/<uuid:submission_id>/decision/', editor_api.admin_venue_submission_decision),
    path('admin/venue-submissions/<uuid:submission_id>/download/', editor_api.admin_venue_submission_download),

    path('admin/smtp/', views.admin_smtp_settings),
    path('admin/smtp/test/', views.admin_smtp_test),
]
