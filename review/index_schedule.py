"""Django-Q schedules for the venue index (build plan step 2).

Two schedules, installed and removed together:
  * monthly, on the 1st: refresh the catalogue (OpenAlex) and check records against Crossref/DOAJ;
  * daily: finish any Crossref/DOAJ checks the monthly run left over. Exits at once when none are left.
"""
from datetime import datetime, time
from zoneinfo import ZoneInfo

from django.utils import timezone

from .discovery_schedule import default_timezone, next_run_at, validate_timezone

TASK = 'review.tasks.run_venue_index_task'
MONTHLY_NAME = 'flexee-venue-index-monthly'
DAILY_NAME = 'flexee-venue-index-daily-checks'
MONTHLY_AT = (3, 30)  # local time on the 1st
DAILY_AT = (4, 30)


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
    return {
        'enabled': bool(monthly),
        'next_full_refresh': monthly.next_run.isoformat() if monthly and monthly.next_run else None,
        'next_daily_checks': daily.next_run.isoformat() if daily and daily.next_run else None,
    }


def set_index_schedule(*, enabled, tz_name=None):
    from django_q.models import Schedule
    Schedule.objects.filter(func=TASK).exclude(name__in=[MONTHLY_NAME, DAILY_NAME]).delete()
    if not enabled:
        Schedule.objects.filter(name__in=[MONTHLY_NAME, DAILY_NAME]).delete()
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
    return get_index_schedule()
