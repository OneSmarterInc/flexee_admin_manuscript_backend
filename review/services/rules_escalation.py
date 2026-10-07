"""Local model first, escalation only on evidence (build plan step 6, section 8).

No learned router: the page validator already knows whether an extraction is good enough, so it
decides. For rule extraction:

    1. local model                       adequate? done
    2. one local retry, told what failed adequate? done
    3. cloud model (Anthropic)           only if VENUE_INDEX_ESCALATE turns it on (off by default:
                                         local models only), within the per-run ceiling and AI budget

Each attempt is stored as a RulesAttempt (stage, model, outcome, missing fields), and cloud calls go
through the existing AI usage accounting under their own operation name, so escalation cost is
visible. The number that decides everything is the escalation rate per field: under 20 percent the
local model pays for itself; above it, that field should move to the cloud.
"""
import os
import uuid
from dataclasses import dataclass, field, replace
from datetime import timedelta

from django.db.models import Sum
from django.utils import timezone

OPERATIONS = {'local': 'index_rules_local', 'local_retry': 'index_rules_retry', 'cloud': 'index_rules_cloud'}
DEFAULT_REQUIRED_FIELDS = ('aims_scope', 'article_types')
FIELD_LABELS = {
    'quotes': 'Quoted evidence',
    'confidence': 'Confidence',
    'aims_scope': 'Aims and scope',
    'article_types': 'Article types',
    'structured_desk_rejection_rules': 'Limits',
    'required_submission_items': 'Required items',
    'submission_types': 'Submission types',
}
MOVE_TO_CLOUD_RATE = 0.20


def _env_int(name, default, low, high):
    try:
        return max(low, min(int(os.getenv(name, str(default))), high))
    except ValueError:
        return default


def required_fields():
    raw = os.getenv('VENUE_INDEX_REQUIRED_FIELDS', '')
    chosen = tuple(f.strip() for f in raw.split(',') if f.strip() in FIELD_LABELS)
    return chosen or DEFAULT_REQUIRED_FIELDS


def _value(item, name):
    return item.get(name) if isinstance(item, dict) else getattr(item, name, None)


def missing_fields(item, min_confidence):
    """What makes an extraction inadequate, as field codes ('quotes' and 'confidence' are validation)."""
    missing = []
    if not _value(item, 'source_evidence'):
        missing.append('quotes')
    elif (_value(item, 'confidence') or 0) < min_confidence:
        missing.append('confidence')
    missing += [f for f in required_fields() if not _value(item, f)]
    return missing


def feedback_for(reason, missing):
    """What the retry is told. Short, concrete, about the page."""
    asks = []
    if 'quotes' in missing:
        asks.append('every rule needs an evidence_text copied word for word from the page')
    for name in missing:
        if name in ('aims_scope', 'article_types', 'structured_desk_rejection_rules', 'required_submission_items'):
            asks.append(f'look again for {FIELD_LABELS[name].lower()}')
    detail = '; '.join(asks) or 'read the pages again carefully'
    return (f'Your previous answer for these pages was not usable ({reason or "missing information"}). '
            f'This time: {detail}. If the pages really do not say it, leave it empty.')


@dataclass
class Escalation:
    """Escalation policy and counters for one run."""
    cloud_enabled: bool
    cloud_left: int
    retry_model: str = ''
    cloud_blocked: str = ''
    retried: int = 0
    escalated: int = 0
    notes: list = field(default_factory=list)

    @classmethod
    def from_env(cls):
        mode = os.getenv('VENUE_INDEX_ESCALATE', 'off').strip().lower()  # local only unless turned on
        has_key = bool(os.getenv('ANTHROPIC_API_KEY', '').strip())
        enabled = mode == 'anthropic' or (mode == 'auto' and has_key)
        return cls(cloud_enabled=enabled, cloud_left=_env_int('VENUE_INDEX_ESCALATIONS_PER_RUN', 10, 0, 1000),
                   retry_model=os.getenv('VENUE_INDEX_RETRY_OLLAMA_MODEL', '').strip())

    def cloud_allowed(self):
        return self.cloud_enabled and self.cloud_left > 0 and not self.cloud_blocked

    def describe(self):
        if not self.cloud_enabled:
            return 'escalation: one local retry; cloud off'
        return f'escalation: one local retry, then Anthropic (up to {self.cloud_left} this run)'


def _plan(config, escalation):
    stages = ['local']
    if config.ai_provider == 'ollama':
        stages.append('local_retry')
        if escalation.cloud_allowed():
            stages.append('cloud')
    return stages


def _call(stage, pages, config, *, extractor, feedback, escalation):
    """Run one stage. Returns (provider, model, raw). Custom extractors (tests) get (pages, config)."""
    from . import venue_discovery as vd
    if stage == 'cloud':
        config = replace(config, ai_provider='anthropic')
    if extractor is not None:
        return config.ai_provider, '', extractor(pages, config)
    model = escalation.retry_model if stage == 'local_retry' and escalation.retry_model else None
    if config.ai_provider == 'ollama':
        model = model or vd.local_ai_settings()['model']
    raw = vd.extract_with_ai(pages, config, operation=OPERATIONS[stage], feedback=feedback, model=model)
    return config.ai_provider, model or os.getenv('ANTHROPIC_MODEL', ''), raw


def extract_with_escalation(record, pages, config, hints, *, escalation, min_confidence, extractor=None, run=None):
    """Extract, validate and escalate. Returns the best validated candidate dict (or None)."""
    from ..ai_usage import AIBudgetExceeded
    from ..models import RulesAttempt
    from . import venue_discovery as vd
    from .index_rules import RulesModelUnavailable, rules_found

    chain = uuid.uuid4()
    best, best_key, attempts = None, None, []
    feedback = ''
    for number, stage in enumerate(_plan(config, escalation), 1):
        if stage == 'cloud' and not escalation.cloud_allowed():
            break
        provider, model, error = config.ai_provider, '', ''
        try:
            provider, model, raw = _call(stage, pages, config, extractor=extractor, feedback=feedback,
                                         escalation=escalation)
        except vd.DiscoveryModelUnavailable as exc:
            if stage == 'local':
                raise RulesModelUnavailable(str(exc)) from exc  # the main model is down: stop the run
            if stage == 'local_retry':
                # The retry failed (often a larger retry model too heavy for the machine). Keep the first
                # result, and stop using the separate retry model for the rest of the run.
                raw, error = None, f'Local retry failed: {exc}'[:300]
                if escalation.retry_model:
                    escalation.notes.append(f'Retry model {escalation.retry_model} failed ({exc}); '
                                            'retries use the main model for the rest of this run.')
                    escalation.retry_model = ''
            else:
                escalation.cloud_blocked, raw, error = str(exc), None, str(exc)
        except AIBudgetExceeded as exc:
            escalation.cloud_blocked, raw, error = f'AI budget reached: {exc}', None, f'AI budget reached: {exc}'
        except vd.DiscoveryExtractionError as exc:
            raw, error = {}, f'The model returned unusable output: {exc}'
        except Exception as exc:  # a cloud SDK error must not stop the run
            if stage != 'cloud':
                raise
            escalation.cloud_blocked, raw, error = str(exc)[:200], None, str(exc)[:200]
        if stage == 'local_retry':
            escalation.retried += 1
        if stage == 'cloud':
            escalation.cloud_left -= 1
            escalation.escalated += 1

        candidate, missing, reason = None, [], error
        if raw is not None:
            try:
                candidate = vd.candidate_from_raw(raw, pages, hints)
                ok, why = rules_found(candidate, min_confidence)
                missing = missing_fields(candidate, min_confidence)
                reason = error or why or ('Missing: ' + ', '.join(FIELD_LABELS[m] for m in missing) if missing else '')
            except vd.DiscoveryExtractionError as exc:
                candidate, reason = None, str(exc)
        adequate = candidate is not None and rules_found(candidate, min_confidence)[0] and not missing
        outcome = 'adequate' if adequate else ('error' if candidate is None else 'inadequate')
        attempts.append(RulesAttempt(chain=chain, record=record, run=run, attempt=number, stage=stage,
                                     provider=provider, model=model[:200], outcome=outcome, reason=reason[:300],
                                     missing_fields=missing if candidate is not None else []))
        if candidate is not None:
            key = (rules_found(candidate, min_confidence)[0], -len(missing), len(candidate.get('source_evidence') or []))
            if best_key is None or key > best_key:
                best, best_key = candidate, key
        if adequate:
            break
        feedback = feedback_for(reason, missing)
    for previous in attempts[:-1]:
        previous.escalated = True
    RulesAttempt.objects.bulk_create(attempts)
    return best, attempts


# ---------------------------------------------------------------------------
# The number that decides everything (section 8.1)
# ---------------------------------------------------------------------------

def escalation_stats(days=30, now=None):
    """Escalation rate per field over the last `days`, from first local attempts, plus where reads ended."""
    from ..models import AIUsageEvent, RulesAttempt
    now = now or timezone.now()
    since = now - timedelta(days=days)
    attempts = list(RulesAttempt.objects.filter(created_at__gte=since, job='rule_extraction')
                    .values('chain', 'attempt', 'stage', 'outcome', 'missing_fields'))
    chains = {}
    for a in attempts:
        chains.setdefault(a['chain'], []).append(a)
    total = len(chains)
    resolved = {'local': 0, 'local_retry': 0, 'cloud': 0, 'unresolved': 0}
    field_counts = {}
    for chain in chains.values():
        chain.sort(key=lambda a: a['attempt'])
        first = chain[0]
        for name in first['missing_fields'] or []:
            field_counts[name] = field_counts.get(name, 0) + 1
        done = next((a['stage'] for a in chain if a['outcome'] == 'adequate'), 'unresolved')
        resolved[done] += 1
    fields = [{'field': name, 'label': FIELD_LABELS.get(name, name), 'count': count,
               'rate': round(count / total, 3) if total else 0.0,
               'move_to_cloud': bool(total) and count / total > MOVE_TO_CLOUD_RATE}
              for name, count in sorted(field_counts.items(), key=lambda kv: -kv[1])]
    cloud = AIUsageEvent.objects.filter(operation=OPERATIONS['cloud'], created_at__gte=since)
    return {
        'days': days,
        'reads': total,
        'resolved': resolved,
        'escalation_rate': round((total - resolved['local']) / total, 3) if total else 0.0,
        'fields': fields,
        'threshold': MOVE_TO_CLOUD_RATE,
        'cloud_calls': cloud.count(),
        'cloud_cost_usd': float(cloud.aggregate(total=Sum('actual_cost_usd'))['total'] or 0),
        'cloud_enabled': Escalation.from_env().cloud_enabled,
        'required_fields': list(required_fields()),
    }
