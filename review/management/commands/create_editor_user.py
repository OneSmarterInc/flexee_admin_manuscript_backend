import base64
import secrets
from urllib.parse import quote
from django.core.management.base import BaseCommand
from django.db import transaction
from review.models import EditorUser, Organization, Membership
from review.auth import hash_password

class Command(BaseCommand):
    help = 'Create an EditorUser and assign a Membership'

    def add_arguments(self, parser):
        parser.add_argument('email', type=str, help='Email address of the user')
        parser.add_argument('--org_id', type=str, help='UUID of the Organization')
        parser.add_argument('--role', type=str, choices=['owner', 'editor', 'viewer'], help='Role for the membership')
        parser.add_argument('--platform_superuser', action='store_true', help='Set if the user is a platform superuser')
        parser.add_argument('--password', type=str, help='Provide password (otherwise randomly generated)')

    def handle(self, *args, **options):
        email = options['email']
        org_id = options.get('org_id')
        role = options.get('role')
        is_superuser = options['platform_superuser']
        
        if not org_id and not is_superuser:
            self.stderr.write(self.style.ERROR('Must provide --org_id unless --platform_superuser is set.'))
            return
            
        if org_id and not role:
            self.stderr.write(self.style.ERROR('Must provide --role when --org_id is set.'))
            return
            
        org = None
        if org_id:
            try:
                org = Organization.objects.get(id=org_id)
            except Organization.DoesNotExist:
                self.stderr.write(self.style.ERROR(f'Organization {org_id} not found.'))
                return

        password = options.get('password') or secrets.token_urlsafe(18)
        
        needs_totp = is_superuser or role in ['owner', 'editor']
        totp_secret = ''
        if needs_totp:
            totp_secret = base64.b32encode(secrets.token_bytes(20)).decode('ascii').rstrip('=')

        with transaction.atomic():
            user, created = EditorUser.objects.get_or_create(
                email=email,
                defaults={
                    'password_hash': hash_password(password),
                    'totp_secret': totp_secret,
                    'platform_superuser': is_superuser
                }
            )
            
            if not created:
                self.stdout.write(self.style.WARNING(f'User {email} already exists.'))
                if needs_totp and not user.totp_secret:
                    user.totp_secret = totp_secret
                    user.save(update_fields=['totp_secret'])
                    self.stdout.write(self.style.WARNING(f'Generated new TOTP secret for existing user {email}.'))
                else:
                    totp_secret = user.totp_secret
                    
            if org:
                Membership.objects.update_or_create(
                    user=user,
                    organization=org,
                    defaults={'role': role}
                )

        self.stdout.write(self.style.SUCCESS(f'Successfully processed user {email}'))
        
        if created:
            self.stdout.write(f'Password: {password}')
            
        if needs_totp and totp_secret:
            label = quote(f'Flexee Admin:{email}')
            issuer = quote('Flexee Manuscript Admin')
            uri = f'otpauth://totp/{label}?secret={totp_secret}&issuer={issuer}&algorithm=SHA1&digits=6&period=30'
            self.stdout.write(f'\nAuthenticator setup URI:\n{uri}\n')
            
            try:
                import qrcode
                qr = qrcode.QRCode()
                qr.add_data(uri)
                qr.make(fit=True)
                qr.print_ascii(out=self.stdout)
            except ImportError:
                self.stdout.write('\nInstall "qrcode" package to display a QR code in the terminal.')
