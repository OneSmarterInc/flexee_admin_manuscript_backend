import os
from django.conf import settings
from django.core.mail import EmailMultiAlternatives, get_connection
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


def _send(to, subject, body, bcc=None):
    if not to:
        return {'skipped': True, 'reason': 'No recipient configured'}
        
    connection = None
    smtp_settings = SMTPSettings.objects.first()
    
    from_email = settings.DEFAULT_FROM_EMAIL
    reply_to = []
    
    if smtp_settings and smtp_settings.host:
        connection = get_connection(
            backend='django.core.mail.backends.smtp.EmailBackend',
            host=smtp_settings.host,
            port=smtp_settings.port,
            username=smtp_settings.username,
            password=smtp_settings.password,
            use_tls=smtp_settings.use_tls,
            use_ssl=smtp_settings.use_ssl,
            fail_silently=False,
        )
        
        if smtp_settings.sender_email:
            if smtp_settings.sender_name:
                from_email = f"{smtp_settings.sender_name} <{smtp_settings.sender_email}>"
            else:
                from_email = smtp_settings.sender_email
        else:
            if smtp_settings.sender_name:
                from_email = f"{smtp_settings.sender_name} <{smtp_settings.username}>"
            else:
                from_email = smtp_settings.username
                
        if smtp_settings.reply_to_email:
            reply_to = [smtp_settings.reply_to_email]
            
    message = EmailMultiAlternatives(
        subject=subject, 
        body=body, 
        from_email=from_email, 
        to=[to], 
        connection=connection,
        reply_to=reply_to if reply_to else None
    )
    message.attach_alternative(build_html_email(subject, body), "text/html")
    sent = message.send(fail_silently=False)
    
    # Send separate explicit copies to BCC recipients to bypass SMTP restrictions
    if bcc:
        for bcc_email in bcc:
            bcc_email = bcc_email.strip()
            # Don't send a duplicate to the author if they are in the BCC list
            if bcc_email and bcc_email.lower() != to.strip().lower():
                bcc_message = EmailMultiAlternatives(
                    subject=subject, 
                    body=body, 
                    from_email=from_email, 
                    to=[bcc_email], 
                    connection=connection,
                    reply_to=reply_to if reply_to else None
                )
                bcc_message.attach_alternative(build_html_email(subject, body), "text/html")
                bcc_message.send(fail_silently=True)
                
    return {'recipient': to, 'sent': bool(sent)}


def send_review_emails(submission, result):
    default_review = os.getenv('DEFAULT_REVIEW_EMAIL', '').strip()
    editor = default_review or os.getenv('EDITOR_EMAIL', 'editor@flexee.org').strip()
    author_target = submission.author_email.strip() or default_review
    detail = {'author': None, 'editor': None}
    smtp_settings = SMTPSettings.objects.first()
    bcc_list = []
    if smtp_settings and smtp_settings.admin_notification_emails:
        bcc_list = [email.strip() for email in smtp_settings.admin_notification_emails.split(',') if email.strip()]
        
    body = (
        f"Dear {submission.author_name},\n\n"
        f"Thank you for submitting \"{submission.title}\" to Flexee.\n\n"
        f"We have successfully received your submission and it is currently under editorial review. Our team will evaluate your work based on our review guidelines and overall suitability.\n\n"
        f"We will update you once the review process is complete.\n\n"
        f"Thank you for sharing your work with us.\n\n"
        f"Best regards,\nFlexee Editorial Team\neditor@flexee.org"
    )

    if author_target:
        detail['author'] = _send(
            author_target,
            'Your Submission Has Been Received – Flexee Editorial Review',
            body,
            bcc=bcc_list
        )
    elif bcc_list:
        for bcc_email in bcc_list:
            if bcc_email.strip():
                _send(
                    bcc_email.strip(),
                    'Your Submission Has Been Received – Flexee Editorial Review',
                    body
                )
        detail['author'] = {'recipient': 'Admins Only (No Author Email)', 'sent': True}
    detail['editor'] = _send(
        editor,
        f"First-gate review: {submission.title} — {result['decision']}",
        f"Author: {submission.author_name}{f' <{submission.author_email}>' if submission.author_email else ''}\n"
        f"Title: {submission.title}\nSubmission: {submission.id}\n\n{result['editor_summary']}",
    )
    return {'status': 'sent', 'detail': detail, 'notified_at': timezone.now()}


def send_acceptance_email(submission, message):
    target = submission.author_email.strip()
    
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
    
    smtp_settings = SMTPSettings.objects.first()
    bcc_list = []
    if smtp_settings and smtp_settings.admin_notification_emails:
        bcc_list = [email.strip() for email in smtp_settings.admin_notification_emails.split(',') if email.strip()]
        
    if target:
        detail = _send(target, 'Your Submission Has Been Approved – Flexee', body, bcc=bcc_list)
    elif bcc_list:
        for bcc_email in bcc_list:
            if bcc_email.strip():
                _send(bcc_email.strip(), 'Your Submission Has Been Approved – Flexee', body)
        detail = {'recipient': 'Admins Only (No Author Email)', 'sent': True}
    else:
        return {'status': 'error', 'detail': {'error': 'No author email provided and no admins configured'}}
        
    return {'status': 'sent' if detail.get('sent') else 'error', 'detail': detail, 'notified_at': timezone.now()}


def send_rejection_email(submission, reason):
    target = submission.author_email.strip()
    
    body = (
        f"Dear {submission.author_name},\n\n"
        f"Thank you for submitting \"{submission.title}\" to Flexee and for giving us the opportunity to review your work.\n\n"
        f"After careful consideration, we have decided not to proceed with your submission at this stage.\n\n"
        f"Editorial Feedback:\n{reason}\n\n"
        f"We appreciate the time and effort invested in your submission and encourage you to continue developing your work.\n\n"
        f"Thank you for your interest in Flexee.\n\n"
        f"Best regards,\nFlexee Editorial Team\neditor@flexee.org"
    )
    
    smtp_settings = SMTPSettings.objects.first()
    bcc_list = []
    if smtp_settings and smtp_settings.admin_notification_emails:
        bcc_list = [email.strip() for email in smtp_settings.admin_notification_emails.split(',') if email.strip()]
        
    if target:
        detail = _send(target, 'Update Regarding Your Submission – Flexee Editorial Review', body, bcc=bcc_list)
    elif bcc_list:
        for bcc_email in bcc_list:
            if bcc_email.strip():
                _send(bcc_email.strip(), 'Update Regarding Your Submission – Flexee Editorial Review', body)
        detail = {'recipient': 'Admins Only (No Author Email)', 'sent': True}
    else:
        return {'status': 'error', 'detail': {'error': 'No author email provided and no admins configured'}}
        
    return {'status': 'sent' if detail.get('sent') else 'error', 'detail': detail, 'notified_at': timezone.now()}


def send_custom_email(submission, subject, body):
    target = submission.author_email.strip()
    
    smtp_settings = SMTPSettings.objects.first()
    bcc_list = []
    if smtp_settings and smtp_settings.admin_notification_emails:
        bcc_list = [email.strip() for email in smtp_settings.admin_notification_emails.split(',') if email.strip()]
        
    if target:
        detail = _send(target, subject, body, bcc=bcc_list)
    elif bcc_list:
        for bcc_email in bcc_list:
            if bcc_email.strip():
                _send(bcc_email.strip(), subject, body)
        detail = {'recipient': 'Admins Only (No Author Email)', 'sent': True}
    else:
        return {'status': 'error', 'detail': {'error': 'No author email provided and no admins configured'}}
        
    return {'status': 'sent' if detail.get('sent') else 'error', 'detail': detail, 'notified_at': timezone.now()}
