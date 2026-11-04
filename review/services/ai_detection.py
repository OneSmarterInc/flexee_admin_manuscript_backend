"""Independent AI-pattern checks, isolated from readiness and venue eligibility."""
import json
import hashlib
import logging
import math
import os
import subprocess
import tempfile
import uuid
from datetime import timedelta
from pathlib import Path
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from review.models import ManuscriptAIDetection, ManuscriptVersion

logger = logging.getLogger(__name__)


def enqueue_detection(manuscript, *, retry=False):
    if os.getenv('AI_DETECTOR_ENABLED', 'false').lower() != 'true' or not manuscript.current_version_id:
        return
    version_id = manuscript.current_version_id
    def enqueue():
        try:
            version = ManuscriptVersion.objects.get(pk=version_id)
            if version.content_purged_at or not version.file:
                return
            with transaction.atomic():
                row, created = ManuscriptAIDetection.objects.select_for_update().get_or_create(
                    version=version, defaults={'sha256': version.sha256})
                if not created and row.sha256 == version.sha256 and not (retry and row.ai_status in ('FAILED', 'NOT_CHECKED')):
                    return
                if not created:
                    row.sha256 = version.sha256
                    row.token = uuid.uuid4()
                    row.ai_status = 'PENDING'
                    row.ai_score = None
                    row.ai_analysis_label = ''
                    row.ai_processed_at = row.started_at = None
                    row.result = {}
                    row.save()
            from django_q.tasks import async_task
            try:
                async_task('review.services.ai_detection.run_detection', row.pk, str(row.token),
                           cluster='flexee_detector', timeout=660, sync=False)
            except Exception:
                ManuscriptAIDetection.objects.filter(pk=row.pk, token=row.token).update(ai_status='FAILED',
                    ai_processed_at=timezone.now(), result={'reason':'Detection could not be queued.'})
                raise
        except Exception:
            logger.exception('Could not queue AI detection for version %s', version_id)
    transaction.on_commit(enqueue, robust=True)


def invoke_detector(version):
    root = Path(settings.BASE_DIR)
    default_python = root / '.venv-ai' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    python = os.getenv('AI_DETECTOR_PYTHON', str(default_python))
    model = os.getenv('AI_DETECTOR_MODEL_DIR', str(root/'var/ai_detector'))
    runner = root/'review/services/ai_detector_runtime/runner.py'
    with tempfile.TemporaryDirectory(prefix='flexee-detection-') as folder:
        path = Path(folder)/('manuscript'+Path(version.filename).suffix.lower())
        with version.file.open('rb') as source, path.open('wb') as dest:
            size = 0
            digest = hashlib.sha256()
            while True:
                chunk = source.read(1024*1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > 100*1024*1024:
                    raise ValueError('File too large')
                digest.update(chunk)
                dest.write(chunk)
        if digest.hexdigest() != version.sha256:
            raise ValueError("Stored file does not match manuscript version")
        proc = subprocess.run([python, str(runner), str(path), model], capture_output=True,
                              timeout=600, text=True, encoding='utf-8',
                              creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        if proc.returncode:
            raise RuntimeError('Detector subprocess failed (exit %s)' % proc.returncode)
        return json.loads(proc.stdout)


def run_detection(row_id, token):
    # A killed worker may reclaim its lease after the queue retry interval.
    with transaction.atomic():
        row = ManuscriptAIDetection.objects.select_for_update().filter(pk=row_id, token=token).first()
        if not row or row.ai_status in ('COMPLETED','FAILED','NOT_CHECKED'):
            return
        if row.ai_status == 'PROCESSING' and row.started_at and row.started_at > timezone.now()-timedelta(seconds=680):
            return
        row.ai_status, row.started_at = 'PROCESSING', timezone.now()
        row.save(update_fields=['ai_status','started_at'])
    try:
        version = ManuscriptVersion.objects.get(pk=row.version_id)
        if version.sha256 != row.sha256 or version.content_purged_at or version.manuscript.content_purged_at:
            raise ValueError('Manuscript content changed or was removed')
        result = invoke_detector(version)
        status = result.get('status')
        score = None
        label = ''
        if status == 'estimated':
            score = result.get('ai_pattern_score')
            if isinstance(score, bool) or not isinstance(score, (int,float)) or not math.isfinite(score) or not 0 <= score <= 100:
                raise ValueError('Invalid detector score')
            label = {'Leans AI-generated':'Likely AI-written','Leans human-written':'Likely human-written'}.get(result.get('label'),'Uncertain')
            state = 'COMPLETED'
        elif status == 'not_checked':
            state = 'NOT_CHECKED'
        else:
            raise ValueError('Detector could not complete analysis')
        # No raw excerpts or internal errors are stored or exposed.
        result.pop('error', None)
        for passage in result.get('passages', []):
            passage.pop('text_excerpt', None)
        saved = ManuscriptAIDetection.objects.filter(pk=row_id, token=token, version__sha256=row.sha256,
            version__content_purged_at__isnull=True, version__manuscript__content_purged_at__isnull=True).update(
            ai_status=state, ai_score=score, ai_analysis_label=label,
            ai_processed_at=timezone.now(), result=result)
        if not saved:
            ManuscriptAIDetection.objects.filter(pk=row_id, token=token).update(ai_status='FAILED', ai_score=None, ai_processed_at=timezone.now(), result={'reason':'Content changed or was removed during analysis.'})
    except Exception:
        logger.exception('AI detection failed for result %s', row_id)
        ManuscriptAIDetection.objects.filter(pk=row_id, token=token).update(ai_status='FAILED',
            ai_score=None, ai_analysis_label='', ai_processed_at=timezone.now(),
            result={'reason':'AI-writing detection could not complete.'})


def detection_payload(manuscript):
    row = ManuscriptAIDetection.objects.filter(version_id=manuscript.current_version_id,
        sha256=manuscript.manuscript_sha256).first() if not manuscript.content_purged_at else None
    return {
        'ai_score': row.ai_score if row else None,
        'ai_status': row.ai_status if row else 'NOT_CHECKED',
        'ai_analysis_label': row.ai_analysis_label if row else '',
        'ai_processed_at': row.ai_processed_at.isoformat() if row and row.ai_processed_at else None,
        'ai_coverage': row.result.get('coverage') if row else None,
        'ai_model': row.result.get('model') if row else None,
        'ai_reason': row.result.get('reason','') if row else '',
    }
