"""Django-Q schedules for the venue index (build plan steps 2 and 7).

Installed and removed together:
  * monthly, on the 1st: refresh the catalogue (OpenAlex) and check records against Crossref/DOAJ;
  * daily: finish any Crossref/DOAJ checks the monthly run left over. Exits at once when none are left;
  * daily: re-confirm open calls for papers on venues not checked for 6 days (weekly per venue, no AI);
  * weekly: read rules for journals that are due (new ones, and live ones every 90 days; local AI).
    VENUE_INDEX_RULES_SCHEDULE=off leaves this one out (for example on a machine without Ollama).
"""
import os
from datetime import datetime, time
from zoneinfo import ZoneInfo

from django.utils import timezone

from .discovery_schedule import default_timezone, next_run_at, validate_timezone

TASK = 'review.tasks.run_venue_index_task'
MONTHLY_NAME = 'flexee-venue-index-monthly'
DAILY_NAME = 'flexee-venue-index-daily-checks'
MONTHLY_AT = (3, 30)  # local time on the 1st
DAILY_AT = (4, 30)
CALLS_NAME = 'flexee-venue-index-daily-calls'
RULES_NAME = 'flexee-venue-index-weekly-rules'
CALLS_AT = (5, 0)
RULES_AT = (1, 30)


def rules_schedule_enabled():
    return os.getenv('VENUE_INDEX_RULES_SCHEDULE', 'weekly').strip().lower() not in {'off', 'false', '0', 'no'}


def first_of_next_month(hour, minute, tz_name, now=None):
    zone = ZoneInfo(tz_name)
    local_now = (now or timezone.now()).astimezone(zone)
    candidate = datetime.combine(local_now.date().replace(day=1), time(hour, minute), tzinfo=zone)
    if candidate <= local_now:
        year, month = (local_now.year + 1, 1) if local_now.month == 12 else (local_now.year, local_now.month + 1)
        candidate = datetime(year, month, 1, hour, minute, tzinfo=zone)
    return candidate


def get_index_schedule():
    from django_q.models import Schedule
    monthly = Schedule.objects.filter(name=MONTHLY_NAME).first()
    daily = Schedule.objects.filter(name=DAILY_NAME).first()
    calls = Schedule.objects.filter(name=CALLS_NAME).first()
    rules = Schedule.objects.filter(name=RULES_NAME).first()
    return {
        'enabled': bool(monthly),
        'next_full_refresh': monthly.next_run.isoformat() if monthly and monthly.next_run else None,
        'next_daily_checks': daily.next_run.isoformat() if daily and daily.next_run else None,
        'next_calls_check': calls.next_run.isoformat() if calls and calls.next_run else None,
        'next_rules_read': rules.next_run.isoformat() if rules and rules.next_run else None,
    }


def set_index_schedule(*, enabled, tz_name=None):
    from django_q.models import Schedule
    names = [MONTHLY_NAME, DAILY_NAME, CALLS_NAME, RULES_NAME]
    Schedule.objects.filter(func=TASK).exclude(name__in=names).delete()
    if not enabled:
        Schedule.objects.filter(name__in=names).delete()
        return get_index_schedule()
    tz_name = validate_timezone(tz_name or default_timezone())
    Schedule.objects.update_or_create(name=MONTHLY_NAME, defaults={
        'func': TASK, 'schedule_type': Schedule.MONTHLY, 'repeats': -1,
        'next_run': first_of_next_month(*MONTHLY_AT, tz_name), 'kwargs': repr({'mode': 'full'}),
    })
    Schedule.objects.update_or_create(name=DAILY_NAME, defaults={
        'func': TASK, 'schedule_type': Schedule.DAILY, 'repeats': -1,
        'next_run': next_run_at(*DAILY_AT, tz_name), 'kwargs': repr({'mode': 'enrich'}),
    })
    Schedule.objects.update_or_create(name=CALLS_NAME, defaults={
        'func': TASK, 'schedule_type': Schedule.DAILY, 'repeats': -1,
        'next_run': next_run_at(*CALLS_AT, tz_name), 'kwargs': repr({'mode': 'calls'}),
    })
    if rules_schedule_enabled():
        Schedule.objects.update_or_create(name=RULES_NAME, defaults={
            'func': TASK, 'schedule_type': Schedule.WEEKLY, 'repeats': -1,
            'next_run': next_run_at(*RULES_AT, tz_name), 'kwargs': repr({'mode': 'rules'}),
        })
    else:
        Schedule.objects.filter(name=RULES_NAME).delete()
    return get_index_schedule()
