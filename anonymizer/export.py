"""End-to-end encrypted exports (roadmap step 6): a file only one researcher can open.

This is the Telegram secret-chat model. The researcher makes a key pair on their own machine and sends
only the public half. The tool seals the anonymized table to that public key. Nobody else can open the
file: not the tool, not the utility's server, not another researcher.

How a file is sealed (standard building blocks, nothing home-made):
  1. A fresh one-time key pair is made for this file (X25519).
  2. It is combined with the researcher's public key into a shared secret (Diffie-Hellman), which is
     turned into an AES-256 key (HKDF-SHA256). The one-time private half is then thrown away.
  3. The table, as compressed CSV, is encrypted with AES-256-GCM. Any change to the file is detected.

The researcher's private key file is itself locked with a passphrase that only they know.
"""
from __future__ import annotations

import base64
import datetime as dt
import gzip
import hashlib
import io
import json

import pandas as pd
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

FORMAT = "anon-export-v1"
MIN_PASSPHRASE = 12


class ExportError(RuntimeError):
    """The export file could not be opened: wrong key, wrong passphrase, or the file was changed."""


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _raw(public_key: X25519PublicKey) -> bytes:
    return public_key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def fingerprint(public_pem: bytes) -> str:
    """A short name for a public key, to read out over the phone and compare: 'a1b2-c3d4-e5f6-0789'."""
    digest = hashlib.sha256(_raw(_load_public(public_pem))).hexdigest()[:16]
    return "-".join(digest[i:i + 4] for i in range(0, 16, 4))


def new_keypair(passphrase: str) -> tuple[bytes, bytes]:
    """Make a researcher's key pair. Returns (private key file, public key file) as PEM text.

    The private file is encrypted with the passphrase. Run this on the researcher's own machine.
    """
    if len(passphrase) < MIN_PASSPHRASE:
        raise ValueError(f"the passphrase must be at least {MIN_PASSPHRASE} characters")
    private = X25519PrivateKey.generate()
    private_pem = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                        serialization.BestAvailableEncryption(passphrase.encode("utf-8")))
    public_pem = private.public_key().public_bytes(serialization.Encoding.PEM,
                                                   serialization.PublicFormat.SubjectPublicKeyInfo)
    return private_pem, public_pem


def _load_public(public_pem: bytes) -> X25519PublicKey:
    try:
        key = serialization.load_pem_public_key(public_pem)
    except ValueError as error:
        raise ExportError("that is not a public key file") from error
    if not isinstance(key, X25519PublicKey):
        raise ExportError("the public key is of the wrong kind; make one with 'researcher-keygen'")
    return key


def _derive_key(shared: bytes, one_time_public: bytes, recipient_public: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
                info=FORMAT.encode() + one_time_public + recipient_public).derive(shared)


def _header_bytes(header: dict) -> bytes:
    return json.dumps(header, sort_keys=True, separators=(",", ":")).encode("utf-8")


def seal(data: bytes, recipient_public_pem: bytes, info: dict) -> bytes:
    """Encrypt data so that only the holder of the matching private key can read it."""
    recipient = _load_public(recipient_public_pem)
    one_time = X25519PrivateKey.generate()                      # used for this file only, never stored
    one_time_public = _raw(one_time.public_key())
    key = _derive_key(one_time.exchange(recipient), one_time_public, _raw(recipient))
    nonce = hashlib.sha256(one_time_public).digest()[:12]       # the key is unique per file, so this is safe
    header = {"format": FORMAT, "recipient": fingerprint(recipient_public_pem),
              "one_time_public": _b64(one_time_public), "info": info}
    ciphertext = AESGCM(key).encrypt(nonce, data, _header_bytes(header))   # the header is protected from changes too
    return json.dumps({**header, "ciphertext": _b64(ciphertext)}, indent=1).encode("utf-8")


def open_sealed(file_bytes: bytes, private_pem: bytes, passphrase: str) -> tuple[bytes, dict]:
    """Decrypt an export. Returns (data, info). Raises ExportError on any problem."""
    try:
        envelope = json.loads(file_bytes)
        ciphertext = base64.b64decode(envelope.pop("ciphertext"))
        one_time_public = base64.b64decode(envelope["one_time_public"])
        if envelope.get("format") != FORMAT:
            raise ValueError("unknown format")
    except (ValueError, KeyError, TypeError) as error:
        raise ExportError("this is not an export file made by this tool") from error
    try:
        private = serialization.load_pem_private_key(private_pem, passphrase.encode("utf-8"))
    except (ValueError, TypeError) as error:
        raise ExportError("the private key could not be unlocked: wrong passphrase or not a private key file") from error
    if not isinstance(private, X25519PrivateKey):
        raise ExportError("the private key is of the wrong kind")
    key = _derive_key(private.exchange(X25519PublicKey.from_public_bytes(one_time_public)),
                      one_time_public, _raw(private.public_key()))
    nonce = hashlib.sha256(one_time_public).digest()[:12]
    try:
        data = AESGCM(key).decrypt(nonce, ciphertext, _header_bytes(envelope))
    except InvalidTag as error:
        raise ExportError("this file was not made for this key, or it was changed after it was made") from error
    return data, envelope["info"]


# ------------------------------------------------------------------ tables in and out
def table_to_bytes(table: pd.DataFrame) -> bytes:
    """The table as gzip-compressed UTF-8 CSV."""
    return gzip.compress(table.to_csv(index=False).encode("utf-8"), mtime=0)


def bytes_to_table(data: bytes) -> pd.DataFrame:
    return pd.read_csv(io.BytesIO(gzip.decompress(data)), dtype=str, keep_default_na=False)


def seal_table(table: pd.DataFrame, recipient_public_pem: bytes, cookbook_version: int) -> bytes:
    info = {"rows": len(table), "columns": list(table.columns), "cookbook_version": cookbook_version,
            "created": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}
    return seal(table_to_bytes(table), recipient_public_pem, info)


def open_table(file_bytes: bytes, private_pem: bytes, passphrase: str) -> tuple[pd.DataFrame, dict]:
    data, info = open_sealed(file_bytes, private_pem, passphrase)
    return bytes_to_table(data), info
