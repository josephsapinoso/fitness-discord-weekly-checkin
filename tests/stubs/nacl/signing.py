"""Test stub for PyNaCl's VerifyKey, backed by `cryptography` (real Ed25519)."""
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from nacl.exceptions import BadSignatureError


class VerifyKey:
    def __init__(self, key: bytes):
        self._key = Ed25519PublicKey.from_public_bytes(key)

    def verify(self, message: bytes, signature: bytes) -> bytes:
        try:
            self._key.verify(signature, message)
        except InvalidSignature:
            raise BadSignatureError("Signature was forged or corrupt")
        return message
