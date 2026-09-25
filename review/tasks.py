from django.utils import timezone
from datetime import timedelta
import os
import zipfile
import concurrent.futures
from io import BytesIO
from .models import ReviewJob, Manuscript, VenueSubmission, Submission, ReviewEvent
from .services.author_agents import (
    run_semantic_readiness,
    run_semantic_matching,
    run_venue_assessment,
    AgentInputError,
    AgentExecutionError
)
from .services.review_engine import run_review, extract_text, word_count as wc_fn
from .services.email_service import send_review_emails
from .views import _generate_chapter_summary, _format_chapter_editor_summary, _chapter_figure_count

def run_public_review_task(job_id, submission_id):
    job = ReviewJob.objects.get(id=job_id)
    job.status = 'processing'
    job.save(update_fields=['status'])
    
    try:
        submission = Submission.objects.get(id=submission_id)
        
        content = submission.manuscript_file.read()
        review_content = content
        review_filename = submission.manuscript_filename
        zip_contents = None
        zip_overall_source = None
        
        if review_filename.lower().endswith('.zip'):
            with zipfile.ZipFile(BytesIO(content)) as zf:
                infolist = zf.infolist()
                if len(infolist) > int(os.getenv('ZIP_MAX_FILES', '1000')):
                    raise ValueError('ZIP contains too many files')
                extracted_size = sum([i.file_size for i in infolist])
                if extracted_size > int(os.getenv('ZIP_MAX_EXTRACTED_BYTES', str(100 * 1024 * 1024))):
                    raise ValueError('Extracted ZIP size exceeds limit')

                manuscript_entries = []
                for name in zf.namelist():
                    if name.lower().endswith(('.docx', '.pdf', '.md')) and not name.startswith('__MACOSX') and not os.path.basename(name).startswith('.'):
                        manuscript_entries.append(name)
                if not manuscript_entries:
                    raise ValueError('No .docx, .pdf, or .md file found inside the ZIP archive')

                zip_docs = []
                docs_to_summarize = []
                combined_parts = []
                for index, entry_name in enumerate(manuscript_entries, start=1):
                    entry_bytes = zf.read(entry_name)
                    base_name = os.path.basename(entry_name)
                    try:
                        doc_text = extract_text(entry_bytes, base_name)
                        doc_words = wc_fn(doc_text)
                    except Exception:
                        doc_text = ''
                        doc_words = 0

                    docs_to_summarize.append((base_name, entry_name, doc_words, doc_text))
                    if doc_text.strip():
                        combined_parts.append(
                            f"# ZIP Document {index}: {base_name}\n"
                            f"Source path: {entry_name}\n"
                            f"Word count: {doc_words}\n\n"
                            f"{doc_text.strip()}"
                        )

                if not combined_parts:
                    raise ValueError('No extractable text found inside the ZIP archive')

                max_workers = max(1, min(int(os.getenv('OLLAMA_CHAPTER_WORKERS', '2')), 2))
                with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                    futures = {
                        executor.submit(_generate_chapter_summary, d[3], d[0], d[2]): d
                        for d in docs_to_summarize
                    }
                    results_map = {}
                    for future in concurrent.futures.as_completed(futures):
                        d = futures[future]
                        try:
                            results_map[d[1]] = future.result()
                        except Exception:
                            results_map[d[1]] = _format_chapter_editor_summary(
                                d[0],
                                d[2],
                                _chapter_figure_count(d[3]),
                                model_summary='Chapter/PDF summary generation failed.',
                                key_gaps='The chapter-level summary call failed for this document.',
                                conclusion='A human editor should inspect this chapter/PDF directly.',
                            )

                for d in docs_to_summarize:
                    base_name, entry_name, doc_words, doc_text = d
                    zip_docs.append({
                        'filename': base_name,
                        'path': entry_name,
                        'word_count': doc_words,
                        'preview': results_map.get(entry_name, "(error)"),
                    })

                zip_contents = zip_docs

                combined_text = "\n\n---\n\n".join(combined_parts)
                review_content = combined_text.encode('utf-8')
                review_filename = f"{os.path.splitext(os.path.basename(review_filename))[0]}_combined.md"
                zip_overall_source = {
                    'type': 'combined_zip_text',
                    'document_count': len(zip_docs),
                    'filename': review_filename,
                }

        result = run_review(review_content, review_filename, submission.kind, submission.declared_sim, submission.disclosure)
        submission.status = 'completed'
        submission.completed_at = timezone.now()
        submission.decision = result['decision']
        submission.model = result['model']
        submission.total_words = result['total_words']
        review_record = result['record']
        if zip_contents:
            review_record['zip_contents'] = zip_contents
        if zip_overall_source:
            review_record['zip_overall_summary_source'] = zip_overall_source
        submission.review_record = review_record
        submission.editor_summary = result['editor_summary']
        submission.author_letter = result['author_letter']
        submission.notification_status = 'pending'
        submission.save()
        ReviewEvent.objects.create(submission=submission, event_type='review_completed', detail={'decision': result['decision'], 'model': result['model']})
        
        try:
            delivery = send_review_emails(submission, result)
            submission.notification_status = delivery['status']
            submission.notification_detail = delivery['detail']
            submission.notified_at = delivery['notified_at']
            submission.save(update_fields=['notification_status', 'notification_detail', 'notified_at', 'updated_at'])
            ReviewEvent.objects.create(submission=submission, event_type='notification_sent', detail=delivery['detail'])
        except Exception as email_error:
            email_warning = str(email_error)
            submission.notification_status = 'error'
            submission.notification_detail = {'error': email_warning}
            submission.save(update_fields=['notification_status', 'notification_detail', 'updated_at'])
            ReviewEvent.objects.create(submission=submission, event_type='notification_error', detail={'error': email_warning})
            
        job.status = 'completed'
        job.completed_at = timezone.now()
        job.progress = 100
        job.save(update_fields=['status', 'completed_at', 'progress'])

    except Exception as error:
        job.status = 'failed'
        job.error_message = str(error)
        job.completed_at = timezone.now()
        job.save(update_fields=['status', 'error_message', 'completed_at'])
        
        try:
            submission = Submission.objects.get(id=submission_id)
            submission.status = 'failed'
            submission.completed_at = timezone.now()
            submission.error = {'type': error.__class__.__name__, 'message': str(error)}
            submission.save(update_fields=['status', 'completed_at', 'error', 'updated_at'])
            ReviewEvent.objects.create(submission=submission, event_type='review_failed', detail=submission.error)
        except Exception:
            pass

def run_semantic_readiness_task(job_id, manuscript_id):
    job = ReviewJob.objects.get(id=job_id)
    job.status = 'processing'
    job.save(update_fields=['status'])
    
    try:
        manuscript = Manuscript.objects.get(id=manuscript_id)
        run_semantic_readiness(manuscript)
        
        job.status = 'completed'
        job.completed_at = timezone.now()
        job.progress = 100
        job.save(update_fields=['status', 'completed_at', 'progress'])
    except Exception as e:
        job.status = 'failed'
        job.error_message = str(e)
        job.completed_at = timezone.now()
        job.save(update_fields=['status', 'error_message', 'completed_at'])

def run_semantic_matching_task(job_id, manuscript_id, venue_ids=None):
    job = ReviewJob.objects.get(id=job_id)
    job.status = 'processing'
    job.save(update_fields=['status'])
    
    try:
        manuscript = Manuscript.objects.get(id=manuscript_id)
        run_semantic_matching(manuscript, venue_ids=venue_ids)
        
        job.status = 'completed'
        job.completed_at = timezone.now()
        job.progress = 100
        job.save(update_fields=['status', 'completed_at', 'progress'])
    except Exception as e:
        job.status = 'failed'
        job.error_message = str(e)
        job.completed_at = timezone.now()
        job.save(update_fields=['status', 'error_message', 'completed_at'])

def run_venue_assessment_task(job_id, submission_id):
    job = ReviewJob.objects.get(id=job_id)
    job.status = 'processing'
    job.save(update_fields=['status'])
    
    try:
        submission = VenueSubmission.objects.select_related(
            'manuscript', 'venue', 'venue__organization', 'venue_config'
        ).get(id=submission_id)
        run_venue_assessment(submission)
        
        job.status = 'completed'
        job.completed_at = timezone.now()
        job.progress = 100
        job.save(update_fields=['status', 'completed_at', 'progress'])
    except Exception as e:
        job.status = 'failed'
        job.error_message = str(e)
        job.completed_at = timezone.now()
        job.save(update_fields=['status', 'error_message', 'completed_at'])

def sweep_stuck_jobs_task():
    cutoff = timezone.now() - timedelta(minutes=30)
    stuck_jobs = ReviewJob.objects.filter(status='processing', updated_at__lt=cutoff)
    count = 0
    
    for job in stuck_jobs:
        job.status = 'failed'
        job.error_message = 'Job timed out after 30 minutes.'
        job.completed_at = timezone.now()
        job.save(update_fields=['status', 'error_message', 'completed_at'])
        count += 1
        
        if job.job_type == 'public_review':
            try:
                submission = Submission.objects.get(id=job.reference_id)
                submission.status = 'failed'
                submission.error = {'type': 'TimeoutError', 'message': 'Job timed out after 30 minutes.'}
                submission.completed_at = timezone.now()
                submission.save(update_fields=['status', 'error', 'completed_at', 'updated_at'])
            except Submission.DoesNotExist:
                pass
        elif job.job_type == 'venue_assessment':
            try:
                submission = VenueSubmission.objects.get(id=job.reference_id)
                submission.status = 'failed'
                submission.error_message = 'Job timed out after 30 minutes.'
                submission.completed_at = timezone.now()
                submission.save(update_fields=['status', 'error_message', 'completed_at'])
            except VenueSubmission.DoesNotExist:
                pass

    return f"Swept {count} stuck jobs."
