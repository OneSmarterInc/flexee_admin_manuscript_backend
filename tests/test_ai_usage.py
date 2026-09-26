from decimal import Decimal
from unittest.mock import patch

import pytest
from django.test import Client

from review.ai_usage import (
    AIBudgetExceeded,
    AIUsageConfigurationError,
    ai_usage_snapshot,
    complete_ai_call,
    reserve_ai_call,
)
from review.auth import issue_session
from review.models import AIUsageEvent, EditorUser
from review.services.ai_provider import ai_chat_json


@pytest.mark.django_db
def test_ollama_usage_is_recorded_without_provider_cost(monkeypatch):
    monkeypatch.setenv('AI_PROVIDER', 'ollama')
    monkeypatch.delenv('AI_OLLAMA_INPUT_USD_PER_MILLION', raising=False)
    monkeypatch.delenv('AI_OLLAMA_OUTPUT_USD_PER_MILLION', raising=False)

    with patch(
        'review.services.ai_provider.ollama_chat_json',
        return_value=(
            'qwen-test',
            '{"ok": true}',
            {
                'input_tokens': 120,
                'output_tokens': 30,
                'usage_estimated': False,
            },
        ),
    ):
        model, payload = ai_chat_json(
            'test prompt',
            max_tokens=50,
            operation='semantic_readiness',
        )

    assert model == 'qwen-test'
    assert payload == '{"ok": true}'

    event = AIUsageEvent.objects.get()
    assert event.provider == 'ollama'
    assert event.model == 'qwen-test'
    assert event.operation == 'semantic_readiness'
    assert event.status == 'completed'
    assert event.input_tokens == 120
    assert event.output_tokens == 30
    assert event.total_tokens == 150
    assert event.actual_cost_usd == Decimal('0.000000')
    assert event.priced is False
    assert event.usage_estimated is False


@pytest.mark.django_db
def test_cloud_budget_blocks_before_provider_call_and_auto_falls_back(monkeypatch):
    monkeypatch.setenv('AI_PROVIDER', 'auto')
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'fake-key')
    monkeypatch.setenv('ANTHROPIC_MODEL', 'priced-model')
    monkeypatch.setenv('AI_COST_ENFORCEMENT_ENABLED', 'true')
    monkeypatch.setenv('AI_DAILY_COST_LIMIT_USD', '0.000001')
    monkeypatch.setenv('AI_MONTHLY_COST_LIMIT_USD', '1')
    monkeypatch.setenv('AI_ANTHROPIC_INPUT_USD_PER_MILLION', '10')
    monkeypatch.setenv('AI_ANTHROPIC_OUTPUT_USD_PER_MILLION', '10')

    with patch('review.services.ai_provider._anthropic_chat_json') as anthropic_call, patch(
        'review.services.ai_provider.ollama_chat_json',
        return_value=(
            'qwen-local',
            '{"fallback": true}',
            {
                'input_tokens': 10,
                'output_tokens': 5,
                'usage_estimated': False,
            },
        ),
    ):
        model, payload = ai_chat_json(
            'This prompt must be reserved before a billable cloud call.',
            max_tokens=100,
            operation='venue_assessment',
        )

    anthropic_call.assert_not_called()
    assert model == 'qwen-local'
    assert payload == '{"fallback": true}'
    assert AIUsageEvent.objects.filter(
        provider='anthropic',
        status='blocked',
        operation='venue_assessment',
    ).count() == 1
    assert AIUsageEvent.objects.filter(
        provider='ollama',
        status='completed',
        operation='venue_assessment',
    ).count() == 1


@pytest.mark.django_db
def test_direct_cloud_call_raises_when_cost_ceiling_would_be_exceeded(monkeypatch):
    monkeypatch.setenv('AI_COST_ENFORCEMENT_ENABLED', 'true')
    monkeypatch.setenv('AI_DAILY_COST_LIMIT_USD', '0.001')
    monkeypatch.setenv('AI_MONTHLY_COST_LIMIT_USD', '100')
    monkeypatch.setenv('AI_ANTHROPIC_INPUT_USD_PER_MILLION', '100')
    monkeypatch.setenv('AI_ANTHROPIC_OUTPUT_USD_PER_MILLION', '100')

    with pytest.raises(AIBudgetExceeded, match='Daily AI cost ceiling'):
        reserve_ai_call(
            provider='anthropic',
            model='priced-model',
            operation='manuscript_review',
            estimated_input_tokens=10000,
            max_output_tokens=1000,
        )

    blocked = AIUsageEvent.objects.get(status='blocked')
    assert blocked.actual_cost_usd == Decimal('0.000000')
    assert blocked.estimated_max_cost_usd > Decimal('0')


@pytest.mark.django_db
def test_completed_reservation_releases_worst_case_to_actual_usage(monkeypatch):
    monkeypatch.setenv('AI_COST_ENFORCEMENT_ENABLED', 'true')
    monkeypatch.setenv('AI_DAILY_COST_LIMIT_USD', '10')
    monkeypatch.setenv('AI_MONTHLY_COST_LIMIT_USD', '100')
    monkeypatch.setenv('AI_ANTHROPIC_INPUT_USD_PER_MILLION', '2')
    monkeypatch.setenv('AI_ANTHROPIC_OUTPUT_USD_PER_MILLION', '4')

    reservation = reserve_ai_call(
        provider='anthropic',
        model='priced-model',
        operation='semantic_matching',
        estimated_input_tokens=1000,
        max_output_tokens=1000,
    )
    assert reservation.status == 'reserved'
    assert reservation.estimated_max_cost_usd == Decimal('0.006000')

    complete_ai_call(
        reservation,
        provider='anthropic',
        model='priced-model',
        operation='semantic_matching',
        input_tokens=100,
        output_tokens=50,
        usage_estimated=False,
    )

    reservation.refresh_from_db()
    assert reservation.status == 'completed'
    assert reservation.actual_cost_usd == Decimal('0.000400')
    assert reservation.total_tokens == 150

    snapshot = ai_usage_snapshot()
    assert snapshot['today']['calls'] == 1
    assert snapshot['today']['cost_usd'] == '0.000400'
    assert snapshot['active_reservations']['count'] == 0
    assert snapshot['by_operation'][0]['operation'] == 'semantic_matching'
    assert snapshot['by_operation'][0]['cost_usd'] == '0.000400'


@pytest.mark.django_db
def test_cloud_cost_enforcement_rejects_partial_pricing(monkeypatch):
    monkeypatch.setenv('AI_COST_ENFORCEMENT_ENABLED', 'true')
    monkeypatch.setenv('AI_DAILY_COST_LIMIT_USD', '5')
    monkeypatch.setenv('AI_MONTHLY_COST_LIMIT_USD', '100')
    monkeypatch.setenv('AI_ANTHROPIC_INPUT_USD_PER_MILLION', '2')
    monkeypatch.setenv('AI_ANTHROPIC_OUTPUT_USD_PER_MILLION', '0')

    with pytest.raises(AIUsageConfigurationError, match='no pricing is configured'):
        reserve_ai_call(
            provider='anthropic',
            model='partial-price-model',
            operation='manuscript_review',
            estimated_input_tokens=100,
            max_output_tokens=100,
        )


@pytest.mark.django_db
def test_ai_usage_admin_endpoint_is_platform_superuser_only(monkeypatch):
    monkeypatch.setenv('AI_COST_ENFORCEMENT_ENABLED', 'false')

    admin = EditorUser.objects.create(
        email='ai-admin@example.com',
        password_hash='dummy',
        platform_superuser=True,
    )
    token, _ = issue_session(admin.email)
    client = Client()
    client.cookies['flxee_admin_session'] = token

    response = client.get('/api/admin/ai-usage/')
    assert response.status_code == 200
    assert response['Cache-Control'] == 'no-store'
    data = response.json()
    assert data['today']['calls'] == 0
    assert data['blocked_calls_total'] == 0
    assert data['cost_enforcement_enabled'] is False

    client.cookies.clear()
    unauthorized = client.get('/api/admin/ai-usage/')
    assert unauthorized.status_code == 401


@pytest.mark.django_db
def test_check_ai_usage_fails_when_cloud_is_unbounded(monkeypatch):
    from django.core.management import call_command
    from django.core.management.base import CommandError

    monkeypatch.setenv('AI_PROVIDER', 'anthropic')
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'fake-key')
    monkeypatch.setenv('AI_COST_ENFORCEMENT_ENABLED', 'false')
    monkeypatch.setenv('AI_DAILY_COST_LIMIT_USD', '0')
    monkeypatch.setenv('AI_MONTHLY_COST_LIMIT_USD', '0')
    monkeypatch.setenv('AI_ANTHROPIC_INPUT_USD_PER_MILLION', '1')
    monkeypatch.setenv('AI_ANTHROPIC_OUTPUT_USD_PER_MILLION', '1')

    with pytest.raises(CommandError, match='monetary cost ceiling'):
        call_command('check_ai_usage', fail_if_unbounded=True)
