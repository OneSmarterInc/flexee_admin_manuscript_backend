"""The single daily Django-Q schedule for venue discovery, settable from the admin page."""
import os
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.utils import timezone

SCHEDULE_NAME = 'flexee-venue-discovery-daily'
TASK = 'review.tasks.run_venue_discovery_task'
COMMON_TIMEZONES = ['Asia/Kolkata', 'America/New_York', 'America/Chicago', 'America/Los_Angeles',
                    'Europe/London', 'Europe/Berlin', 'Asia/Singapore', 'Australia/Sydney', 'UTC']


def default_timezone():
    return os.getenv('VENUE_DISCOVERY_TIMEZONE', '').strip() or settings.TIME_ZONE or 'UTC'


def timezone_choices():
    choices = [default_timezone()] + COMMON_TIMEZONES
    return list(dict.fromkeys(choices))


def validate_timezone(name):
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise ValueError(f'Unknown time zone {name!r}.') from exc
    return name


def parse_time(value):
    try:
        hour, minute = [int(part) for part in str(value).strip().split(':')[:2]]
    except (TypeError, ValueError) as exc:
        raise ValueError('Time must look like HH:MM, for example 02:00.') from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError('Time must be between 00:00 and 23:59.')
    return hour, minute


def next_run_at(hour, minute, tz_name, now=None):
    zone = ZoneInfo(tz_name)
    local_now = (now or timezone.now()).astimezone(zone)
    candidate = datetime.combine(local_now.date(), time(hour, minute), tzinfo=zone)
    if candidate <= local_now:
        candidate = datetime.combine(local_now.date() + timedelta(days=1), time(hour, minute), tzinfo=zone)
    return candidate


def _schedule_tz(schedule):
    import ast
    try:
        kwargs = ast.literal_eval(schedule.kwargs or '{}')
        return kwargs.get('schedule_tz') or default_timezone()
    except (ValueError, SyntaxError):
        return default_timezone()


def get_schedule():
    from django_q.models import Schedule
    schedule = Schedule.objects.filter(name=SCHEDULE_NAME).first()
    if not schedule:
        return {'enabled': False, 'time': '02:00', 'timezone': default_timezone(), 'next_run': None,
                'timezones': timezone_choices()}
    tz_name = _schedule_tz(schedule)
    local = schedule.next_run.astimezone(ZoneInfo(tz_name)) if schedule.next_run else None
    return {
        'enabled': True,
        'time': local.strftime('%H:%M') if local else '02:00',
        'timezone': tz_name,
        'next_run': schedule.next_run.isoformat() if schedule.next_run else None,
        'timezones': list(dict.fromkeys([tz_name] + timezone_choices())),
    }


def set_schedule(*, enabled, hour=2, minute=0, tz_name=None):
    """Install, move or remove the one daily discovery schedule. Idempotent."""
    from django_q.models import Schedule
    Schedule.objects.filter(func=TASK).exclude(name=SCHEDULE_NAME).delete()  # never more than one
    if not enabled:
        Schedule.objects.filter(name=SCHEDULE_NAME).delete()
        return get_schedule()
    tz_name = validate_timezone(tz_name or default_timezone())
    Schedule.objects.update_or_create(
        name=SCHEDULE_NAME,
        defaults={
            'func': TASK,
            'schedule_type': Schedule.DAILY,
            'repeats': -1,
            'next_run': next_run_at(hour, minute, tz_name),
            'kwargs': repr({'schedule_tz': tz_name}),
        },
    )
    return get_schedule()
