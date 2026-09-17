import os
from django.conf import settings
from django.core.mail import EmailMultiAlternatives, get_connection
from django.utils import timezone
from django.utils import timezone
from ..models import SMTPSettings


def build_html_email(subject, body):
    paragraphs = "".join([f'<p style="margin: 0 0 16px 0;">{p.strip()}</p>' for p in body.split('\n\n') if p.strip()])
    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #f9f7f4; margin: 0; padding: 40px 20px; color: #1c1917;">
  <div style="max-width: 600px; margin: 0 auto; background: #ffffff; border-radius: 8px; overflow: hidden; box-shadow: 0 4px 15px rgba(0,0,0,0.05);">
    <div style="background-color: #1c1917; padding: 30px; text-align: center;">
      <h1 style="color: #ffffff; font-size: 28px; font-weight: normal; margin: 0; font-family: Georgia, serif;">Flexee <span style="color: #d97757;">Editorial</span></h1>
    </div>
    <div style="padding: 30px; line-height: 1.6; font-size: 16px;">
      <h2 style="margin-top: 0; font-size: 20px; font-family: Georgia, serif; color: #1c1917;">{subject}</h2>
      {paragraphs}
    </div>
    <div style="background-color: #1c1917; padding: 20px; text-align: center; color: #a8a29e; font-size: 13px;">
      Flexee Editorial Team &bull; editor@flexee.org<br>
      <span style="opacity: 0.7;">This is an automated operational notification.</span>
    </div>
  </div>
</body>
</html>"""


def _send(to, subject, body):
    if not to:
        return {'skipped': True, 'reason': 'No recipient configured'}
        
    connection = None
    smtp_settings = SMTPSettings.objects.first()
    if smtp_settings and smtp_settings.host:
        connection = get_connection(
            backend='django.core.mail.backends.smtp.EmailBackend',
            host=smtp_settings.host,
            port=smtp_settings.port,
            username=smtp_settings.username,
            password=smtp_settings.password,
            use_tls=smtp_settings.use_tls,
            fail_silently=False,
        )
        
    message = EmailMultiAlternatives(subject=subject, body=body, from_email=settings.DEFAULT_FROM_EMAIL, to=[to], connection=connection)
    message.attach_alternative(build_html_email(subject, body), "text/html")
    sent = message.send(fail_silently=False)
    return {'recipient': to, 'sent': bool(sent)}


def send_review_emails(submission, result):
    default_review = os.getenv('DEFAULT_REVIEW_EMAIL', '').strip()
    editor = default_review or os.getenv('EDITOR_EMAIL', 'editor@flexee.org').strip()
    author_target = submission.author_email.strip() or default_review
    detail = {'author': None, 'editor': None}
    if author_target:
        body = (
            f"Dear {submission.author_name},\n\n"
            f"Thank you for submitting \"{submission.title}\" to Flexee.\n\n"
            f"We have successfully received your submission and it is currently under editorial review. Our team will evaluate your work based on our review guidelines and overall suitability.\n\n"
            f"We will update you once the review process is complete.\n\n"
            f"Thank you for sharing your work with us.\n\n"
            f"Best regards,\nFlexee Editorial Team\neditor@flexee.org"
        )
        detail['author'] = _send(
            author_target,
            'Your Submission Has Been Received – Flexee Editorial Review',
            body,
        )
    detail['editor'] = _send(
        editor,
        f"First-gate review: {submission.title} — {result['decision']}",
        f"Author: {submission.author_name}{f' <{submission.author_email}>' if submission.author_email else ''}\n"
        f"Title: {submission.title}\nSubmission: {submission.id}\n\n{result['editor_summary']}",
    )
    return {'status': 'sent', 'detail': detail, 'notified_at': timezone.now()}


def send_acceptance_email(submission, message):
    target = submission.author_email.strip()
    if not target:
        return {'status': 'error', 'detail': {'error': 'No author email provided'}}
    
    body = (
        f"Dear {submission.author_name},\n\n"
        f"We are pleased to inform you that your submission \"{submission.title}\" has been approved by the Flexee editorial team.\n\n"
        f"Your work has successfully passed our initial review process, and we will proceed with the next steps. Our team will contact you with further details.\n\n"
    )
    if message:
        body += f"Message from admin:\n{message}\n\n"
    body += (
        f"Thank you for sharing your work with Flexee. We look forward to working with you.\n\n"
        f"Best regards,\nFlexee Editorial Team\neditor@flexee.org"
    )
    detail = _send(target, 'Your Submission Has Been Approved – Flexee', body)
    return {'status': 'sent' if detail.get('sent') else 'error', 'detail': detail, 'notified_at': timezone.now()}


def send_rejection_email(submission, reason):
    target = submission.author_email.strip()
    if not target:
        return {'status': 'error', 'detail': {'error': 'No author email provided'}}
    
    body = (
        f"Dear {submission.author_name},\n\n"
        f"Thank you for submitting \"{submission.title}\" to Flexee and for giving us the opportunity to review your work.\n\n"
        f"After careful consideration, we have decided not to proceed with your submission at this stage.\n\n"
        f"Editorial Feedback:\n{reason}\n\n"
        f"We appreciate the time and effort invested in your submission and encourage you to continue developing your work.\n\n"
        f"Thank you for your interest in Flexee.\n\n"
        f"Best regards,\nFlexee Editorial Team\neditor@flexee.org"
    )
    detail = _send(target, 'Update Regarding Your Submission – Flexee Editorial Review', body)
    return {'status': 'sent' if detail.get('sent') else 'error', 'detail': detail, 'notified_at': timezone.now()}


def send_custom_email(submission, subject, body):
    target = submission.author_email.strip()
    if not target:
        return {'status': 'error', 'detail': {'error': 'No author email provided'}}
    
    detail = _send(target, subject, body)
    return {'status': 'sent' if detail.get('sent') else 'error', 'detail': detail, 'notified_at': timezone.now()}
