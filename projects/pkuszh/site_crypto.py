"""Encoding used by the site's public patient client, not an account credential."""
import base64
import hashlib
import hmac
import json
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

ENC_KEY = b'KINGDEE@2018GOGO'
DEC_KEY = b'KINGDEE@2021MASK'
SIGN_KEY = b'etnGh4nCBXVN6ATaglj8yLYcbgiSOzwBRYzxv9LxiDI='


def decrypt_field(value):
    if not value or ',' not in value:
        return value or ''
    encoded = value.split(',', 1)[1]
    dec = Cipher(algorithms.AES(DEC_KEY), modes.CBC(DEC_KEY)).decryptor()
    padded = dec.update(base64.b64decode(encoded, validate=True)) + dec.finalize()
    unpad = PKCS7(128).unpadder()
    return (unpad.update(padded) + unpad.finalize()).decode('utf-8')


def encrypt_payload(value):
    raw = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    pad = PKCS7(128).padder()
    padded = pad.update(raw) + pad.finalize()
    enc = Cipher(algorithms.AES(ENC_KEY), modes.CBC(ENC_KEY)).encryptor()
    return base64.b64encode(enc.update(padded) + enc.finalize()).decode('ascii')


def sign(value):
    pairs = []
    for k in sorted(value):
        v = value[k]
        if k == 'sign' or v is None or isinstance(v, str) and not v.strip():
            continue
        pairs.append(k + '=' + str(v))
    raw = ('&'.join(pairs) + SIGN_KEY.decode('ascii')).encode('utf-8')
    return hmac.new(SIGN_KEY, raw, hashlib.sha256).hexdigest()
