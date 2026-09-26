from __future__ import annotations

import json
import os
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from datetime import timedelta

from django.db import transaction
from django.db.models import Count, Q, Sum
from django.utils import timezone

from .models import AIBudgetState, AIUsageEvent
from .monitoring import capture_message


ZERO = Decimal('0')
MILLION = Decimal('1000000')
COST_QUANT = Decimal('0.000001')


class AIBudgetExceeded(RuntimeError):
    pass


class AIUsageConfigurationError(RuntimeError):
    pass


def _as_bool(value, default=False):
    if value is None:
        return default
    return str(value).strip().lower() in {'1', 'true', 'yes', 'on'}


def _decimal_env(name: str, default='0') -> Decimal:
    raw = os.getenv(name, default)
    try:
        value = Decimal(str(raw).strip() or default)
    except (InvalidOperation, ValueError):
        value = Decimal(default)
    return max(ZERO, value)


def _int_env(name: str, default: int, minimum=0) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)


def _money(value) -> Decimal:
    return Decimal(value or 0).quantize(COST_QUANT, rounding=ROUND_HALF_UP)


def cost_controls_enabled() -> bool:
    return _as_bool(os.getenv('AI_COST_ENFORCEMENT_ENABLED'), False)


def budget_limits() -> dict:
    return {
        'enabled': cost_controls_enabled(),
        'daily_cost_limit_usd': _decimal_env('AI_DAILY_COST_LIMIT_USD', '0'),
        'monthly_cost_limit_usd': _decimal_env('AI_MONTHLY_COST_LIMIT_USD', '0'),
        'reservation_ttl_minutes': _int_env('AI_BUDGET_RESERVATION_TTL_MINUTES', 60, minimum=5),
    }


def _pricing_json() -> dict:
    raw = os.getenv('AI_MODEL_PRICING_JSON', '').strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def pricing_for(provider: str, model: str) -> dict:
    provider = str(provider or '').strip().lower()
    model = str(model or '').strip()

    configured = _pricing_json()
    row = configured.get(model)
    if not isinstance(row, dict):
        row = configured.get(f'{provider}:default')
    if not isinstance(row, dict):
        row = {}

    prefix = provider.upper().replace('-', '_')
    input_rate = row.get(
        'input_usd_per_million',
        os.getenv(f'AI_{prefix}_INPUT_USD_PER_MILLION', '0'),
    )
    output_rate = row.get(
        'output_usd_per_million',
        os.getenv(f'AI_{prefix}_OUTPUT_USD_PER_MILLION', '0'),
    )
    try:
        input_rate = max(ZERO, Decimal(str(input_rate or 0)))
    except InvalidOperation:
        input_rate = ZERO
    try:
        output_rate = max(ZERO, Decimal(str(output_rate or 0)))
    except InvalidOperation:
        output_rate = ZERO

    return {
        'input_usd_per_million': input_rate,
        'output_usd_per_million': output_rate,
        'priced': input_rate > 0 or output_rate > 0,
    }


def estimate_cost(provider: str, model: str, input_tokens: int, output_tokens: int) -> tuple[Decimal, bool]:
    pricing = pricing_for(provider, model)
    cost = (
        (Decimal(max(0, int(input_tokens or 0))) / MILLION) * pricing['input_usd_per_million']
        + (Decimal(max(0, int(output_tokens or 0))) / MILLION) * pricing['output_usd_per_million']
    )
    return _money(cost), bool(pricing['priced'])


def _period_starts(now):
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = day_start.replace(day=1)
    return day_start, month_start


def _committed_cost_since(start, reservation_cutoff) -> Decimal:
    completed = AIUsageEvent.objects.filter(
        created_at__gte=start,
        status='completed',
    ).aggregate(total=Sum('actual_cost_usd'))['total'] or ZERO
    reserved = AIUsageEvent.objects.filter(
        created_at__gte=max(start, reservation_cutoff),
        status='reserved',
    ).aggregate(total=Sum('estimated_max_cost_usd'))['total'] or ZERO
    return _money(Decimal(completed) + Decimal(reserved))


def _validate_enforcement(provider: str, model: str, estimated_cost: Decimal, priced: bool):
    limits = budget_limits()
    if not limits['enabled']:
        return limits

    if provider in {'ollama', 'mock'} and not priced:
        return limits

    if not priced:
        raise AIUsageConfigurationError(
            f'AI cost enforcement is enabled but no pricing is configured for {provider}/{model}.'
        )
    if limits['daily_cost_limit_usd'] <= 0 and limits['monthly_cost_limit_usd'] <= 0:
        raise AIUsageConfigurationError(
            'AI cost enforcement is enabled but neither AI_DAILY_COST_LIMIT_USD '
            'nor AI_MONTHLY_COST_LIMIT_USD is configured.'
        )
    if estimated_cost <= 0:
        raise AIUsageConfigurationError(
            f'AI cost enforcement could not estimate a positive maximum cost for {provider}/{model}.'
        )
    return limits


def reserve_ai_call(
    *,
    provider: str,
    model: str,
    operation: str,
    estimated_input_tokens: int,
    max_output_tokens: int,
):
    provider = str(provider or 'unknown').strip().lower()[:40]
    model = str(model or '').strip()[:200]
    operation = str(operation or 'ai_chat').strip()[:100] or 'ai_chat'
    estimated_cost, priced = estimate_cost(
        provider,
        model,
        estimated_input_tokens,
        max_output_tokens,
    )
    limits = _validate_enforcement(provider, model, estimated_cost, priced)

    if provider in {'ollama', 'mock'} and not priced:
        return None

    now = timezone.now()
    day_start, month_start = _period_starts(now)
    reservation_cutoff = now - timedelta(minutes=limits['reservation_ttl_minutes'])
    blocked_reason = None
    event = None

    with transaction.atomic():
        AIBudgetState.objects.get_or_create(key='global')
        AIBudgetState.objects.select_for_update().get(key='global')

        if limits['enabled']:
            daily_used = _committed_cost_since(day_start, reservation_cutoff)
            monthly_used = _committed_cost_since(month_start, reservation_cutoff)

            daily_limit = limits['daily_cost_limit_usd']
            monthly_limit = limits['monthly_cost_limit_usd']
            if daily_limit > 0 and daily_used + estimated_cost > daily_limit:
                blocked_reason = (
                    f'Daily AI cost ceiling would be exceeded: '
                    f'committed USD {daily_used:.6f}, reservation USD {estimated_cost:.6f}, '
                    f'limit USD {daily_limit:.6f}.'
                )
            elif monthly_limit > 0 and monthly_used + estimated_cost > monthly_limit:
                blocked_reason = (
                    f'Monthly AI cost ceiling would be exceeded: '
                    f'committed USD {monthly_used:.6f}, reservation USD {estimated_cost:.6f}, '
                    f'limit USD {monthly_limit:.6f}.'
                )

        event = AIUsageEvent.objects.create(
            provider=provider,
            model=model,
            operation=operation,
            status='blocked' if blocked_reason else 'reserved',
            input_tokens=max(0, int(estimated_input_tokens or 0)),
            output_tokens=0,
            total_tokens=max(0, int(estimated_input_tokens or 0)),
            estimated_max_cost_usd=estimated_cost,
            actual_cost_usd=ZERO,
            priced=priced,
            usage_estimated=True,
            error_type='AIBudgetExceeded' if blocked_reason else '',
        )

    if blocked_reason:
        capture_message(
            'AI cost ceiling blocked provider call',
            component='ai_cost',
            operation='budget_block',
            level='warning',
            tags={'provider': provider, 'model': model, 'ai_operation': operation},
        )
        raise AIBudgetExceeded(blocked_reason)

    return event


def complete_ai_call(
    event,
    *,
    provider: str,
    model: str,
    operation: str,
    input_tokens: int,
    output_tokens: int,
    usage_estimated: bool = False,
):
    input_tokens = max(0, int(input_tokens or 0))
    output_tokens = max(0, int(output_tokens or 0))
    actual_cost, priced = estimate_cost(provider, model, input_tokens, output_tokens)

    if event is None:
        return AIUsageEvent.objects.create(
            provider=str(provider or 'unknown')[:40],
            model=str(model or '')[:200],
            operation=str(operation or 'ai_chat')[:100],
            status='completed',
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            estimated_max_cost_usd=actual_cost,
            actual_cost_usd=actual_cost,
            priced=priced,
            usage_estimated=bool(usage_estimated),
        )

    event.provider = str(provider or event.provider)[:40]
    event.model = str(model or event.model)[:200]
    event.status = 'completed'
    event.input_tokens = input_tokens
    event.output_tokens = output_tokens
    event.total_tokens = input_tokens + output_tokens
    event.actual_cost_usd = actual_cost
    event.priced = priced
    event.usage_estimated = bool(usage_estimated)
    event.error_type = ''
    event.save(update_fields=[
        'provider', 'model', 'status', 'input_tokens', 'output_tokens',
        'total_tokens', 'actual_cost_usd', 'priced', 'usage_estimated',
        'error_type', 'updated_at',
    ])
    return event


def fail_ai_call(
    event,
    exc,
    *,
    provider='unknown',
    model='',
    operation='ai_chat',
    estimated_input_tokens=0,
):
    if event is None:
        return AIUsageEvent.objects.create(
            provider=str(provider or 'unknown')[:40],
            model=str(model or '')[:200],
            operation=str(operation or 'ai_chat')[:100],
            status='failed',
            input_tokens=max(0, int(estimated_input_tokens or 0)),
            output_tokens=0,
            total_tokens=max(0, int(estimated_input_tokens or 0)),
            estimated_max_cost_usd=ZERO,
            actual_cost_usd=ZERO,
            priced=pricing_for(provider, model)['priced'],
            usage_estimated=True,
            error_type=exc.__class__.__name__[:100],
        )
    event.status = 'failed'
    event.error_type = exc.__class__.__name__[:100]
    event.actual_cost_usd = ZERO
    event.save(update_fields=['status', 'error_type', 'actual_cost_usd', 'updated_at'])
    return event


def _usage_aggregate(queryset):
    totals = queryset.aggregate(
        calls=Count('id'),
        input_tokens=Sum('input_tokens'),
        output_tokens=Sum('output_tokens'),
        total_tokens=Sum('total_tokens'),
        cost_usd=Sum('actual_cost_usd'),
    )
    return {
        'calls': totals['calls'] or 0,
        'input_tokens': totals['input_tokens'] or 0,
        'output_tokens': totals['output_tokens'] or 0,
        'total_tokens': totals['total_tokens'] or 0,
        'cost_usd': str(_money(totals['cost_usd'] or ZERO)),
    }


def ai_usage_snapshot(*, now=None) -> dict:
    now = now or timezone.now()
    day_start, month_start = _period_starts(now)
    limits = budget_limits()

    completed = AIUsageEvent.objects.filter(status='completed')
    today = _usage_aggregate(completed.filter(created_at__gte=day_start))
    month = _usage_aggregate(completed.filter(created_at__gte=month_start))
    all_time = _usage_aggregate(completed)

    active_reservations = AIUsageEvent.objects.filter(
        status='reserved',
        created_at__gte=now - timedelta(minutes=limits['reservation_ttl_minutes']),
    ).aggregate(count=Count('id'), cost=Sum('estimated_max_cost_usd'))

    provider_rows = []
    for row in (
        completed.values('provider', 'model')
        .annotate(
            calls=Count('id'),
            input_tokens=Sum('input_tokens'),
            output_tokens=Sum('output_tokens'),
            cost_usd=Sum('actual_cost_usd'),
            unpriced_calls=Count('id', filter=Q(priced=False)),
        )
        .order_by('provider', 'model')
    ):
        provider_rows.append({
            'provider': row['provider'],
            'model': row['model'],
            'calls': row['calls'],
            'input_tokens': row['input_tokens'] or 0,
            'output_tokens': row['output_tokens'] or 0,
            'cost_usd': str(_money(row['cost_usd'] or ZERO)),
            'unpriced_calls': row['unpriced_calls'] or 0,
            'currently_priced': pricing_for(row['provider'], row['model'])['priced'],
        })

    operation_rows = []
    for row in (
        completed.values('operation')
        .annotate(
            calls=Count('id'),
            input_tokens=Sum('input_tokens'),
            output_tokens=Sum('output_tokens'),
            cost_usd=Sum('actual_cost_usd'),
        )
        .order_by('operation')
    ):
        operation_rows.append({
            'operation': row['operation'],
            'calls': row['calls'],
            'input_tokens': row['input_tokens'] or 0,
            'output_tokens': row['output_tokens'] or 0,
            'cost_usd': str(_money(row['cost_usd'] or ZERO)),
        })

    daily_limit = limits['daily_cost_limit_usd']
    monthly_limit = limits['monthly_cost_limit_usd']
    reservation_cutoff = now - timedelta(minutes=limits['reservation_ttl_minutes'])
    daily_committed = _committed_cost_since(day_start, reservation_cutoff)
    monthly_committed = _committed_cost_since(month_start, reservation_cutoff)

    return {
        'generated_at': now.isoformat(),
        'cost_enforcement_enabled': limits['enabled'],
        'daily_cost_limit_usd': str(_money(daily_limit)),
        'monthly_cost_limit_usd': str(_money(monthly_limit)),
        'daily_committed_cost_usd': str(daily_committed),
        'monthly_committed_cost_usd': str(monthly_committed),
        'daily_remaining_usd': (
            str(_money(max(ZERO, daily_limit - daily_committed))) if daily_limit > 0 else None
        ),
        'monthly_remaining_usd': (
            str(_money(max(ZERO, monthly_limit - monthly_committed))) if monthly_limit > 0 else None
        ),
        'today': today,
        'month': month,
        'all_time': all_time,
        'active_reservations': {
            'count': active_reservations['count'] or 0,
            'estimated_cost_usd': str(_money(active_reservations['cost'] or ZERO)),
        },
        'blocked_calls_total': AIUsageEvent.objects.filter(status='blocked').count(),
        'failed_calls_total': AIUsageEvent.objects.filter(status='failed').count(),
        'unpriced_cloud_calls_total': completed.exclude(
            provider__in=['ollama', 'mock']
        ).filter(priced=False).count(),
        'by_provider_model': provider_rows,
        'by_operation': operation_rows,
    }
