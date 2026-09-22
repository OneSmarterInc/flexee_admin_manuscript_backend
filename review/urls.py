from django.urls import path
from . import network_api, views
from .api_summary import admin_submission_api_summary

urlpatterns = [
    path('health/', views.health),

    # Legacy Flexee book/article submission flow.
    path('submissions/', views.submit),

    # Agentic scholarly submission network: author-facing API.
    path('author/manuscripts/', network_api.author_create_manuscript),
    path('author/manuscripts/<uuid:manuscript_id>/', network_api.author_manuscript_detail),
    path('author/manuscripts/<uuid:manuscript_id>/readiness/', network_api.author_readiness),
    path('author/manuscripts/<uuid:manuscript_id>/matches/', network_api.author_venue_matches),
    path('author/manuscripts/<uuid:manuscript_id>/venues/<slug:venue_slug>/assessment/', network_api.author_venue_assessment),
    path('author/manuscripts/<uuid:manuscript_id>/choose-venue/', network_api.author_choose_venue),
    path('author/manuscripts/<uuid:manuscript_id>/submit/', network_api.author_submit_packet),
    path('author/manuscripts/<uuid:manuscript_id>/transfer/', network_api.author_transfer_submission),
    path('venues/', network_api.public_venues),

    # Existing admin authentication and legacy review APIs.
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

    # New venue/editor administration APIs.
    path('admin/venues/', network_api.admin_venues),
    path('admin/venues/<uuid:venue_id>/', network_api.admin_venue_detail),
    path('admin/venues/<uuid:venue_id>/feedback/', network_api.admin_venue_feedback),
    path('admin/network-submissions/', network_api.admin_network_submissions),
    path('admin/network-submissions/<uuid:submission_id>/decision/', network_api.admin_network_submission_decision),
]
