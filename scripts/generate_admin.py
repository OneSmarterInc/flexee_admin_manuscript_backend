import argparse
import base64
import hashlib
import secrets
from urllib.parse import quote


def b64u(data):
    return base64.urlsafe_b64encode(data).decode('ascii').rstrip('=')


def hash_password(password):
    n, r, p = 2**14, 8, 1
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode('utf-8'), salt=salt, n=n, r=r, p=p, dklen=32)
    return f'scrypt${n}${r}${p}${b64u(salt)}${b64u(digest)}'


def main():
    parser = argparse.ArgumentParser(description='Generate Flexee admin password/TOTP/session secrets.')
    parser.add_argument('--username', default='admin')
    args = parser.parse_args()
    password = secrets.token_urlsafe(18)
    totp_secret = base64.b32encode(secrets.token_bytes(20)).decode('ascii').rstrip('=')
    session_secret = secrets.token_urlsafe(48)
    label = quote(f'Flexee Manuscript Admin:{args.username}')
    issuer = quote('Flexee Manuscript Admin')
    uri = f'otpauth://totp/{label}?secret={totp_secret}&issuer={issuer}&algorithm=SHA1&digits=6&period=30'
    print('SAVE THESE VALUES SECURELY. The plaintext password is printed only now.\n')
    print(f'ADMIN_USERNAME={args.username}')
    print(f'ADMIN_PASSWORD={password}')
    print(f'ADMIN_PASSWORD_HASH={hash_password(password)}')
    print(f'ADMIN_TOTP_SECRET={totp_secret}')
    print(f'ADMIN_SESSION_SECRET={session_secret}')
    print(f'\nAuthenticator setup URI:\n{uri}')


if __name__ == '__main__':
    main()
