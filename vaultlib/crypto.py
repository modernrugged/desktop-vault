"""Cryptographic core for Desktop Vault.

Design notes
------------
* The password is stretched into a key-encryption key (KEK) with **Argon2id**,
  a memory-hard KDF.  Parameters are calibrated on the machine that creates the
  vault and stored in the (authenticated) header.
* A random 256-bit **master key** is generated once per vault and wrapped with
  AES-256-GCM under the KEK.  Changing the password only re-wraps the master
  key -- no file data is ever re-encrypted.
* Purpose-specific sub-keys are derived from the master key with HKDF-SHA256 so
  the index and the file blobs never share key material.
* File contents are encrypted in 1 MiB chunks with AES-256-GCM under a
  per-file random key.  Each chunk's associated data binds the vault id, the
  blob id, the chunk index and an end-of-stream flag, which makes truncation,
  reordering and cross-file chunk splicing detectable.

There is deliberately no recovery path, no key escrow and no network code.
If the password is lost the data is unrecoverable -- that is the point.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import struct
import time
from dataclasses import dataclass
from typing import BinaryIO, Callable, Optional

from argon2.low_level import Type, hash_secret_raw
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

# ---------------------------------------------------------------- constants --

BLOB_MAGIC = b"SVB1"
CHUNK_SIZE = 1 << 20        # 1 MiB of plaintext per AEAD chunk
PAD_BLOCK = 4096            # plaintext is zero-padded up to a multiple of this
KEY_LEN = 32
SALT_LEN = 16
NONCE_LEN = 12
TAG_LEN = 16
MAX_CHUNK_CT = CHUNK_SIZE + PAD_BLOCK + TAG_LEN + 64   # sanity bound on reads

ProgressCb = Optional[Callable[[int], None]]


class CryptoError(Exception):
    """Raised for corrupt containers, bad tags or malformed blobs."""


# --------------------------------------------------------------- primitives --

def random_bytes(n: int) -> bytes:
    return secrets.token_bytes(n)


def wipe(buf) -> None:
    """Best-effort zeroing of a mutable buffer.

    Python cannot guarantee that no copy survives elsewhere in memory; this
    shortens the window in which a key sits in the process heap.
    """
    if isinstance(buf, bytearray):
        for i in range(len(buf)):
            buf[i] = 0


# ---------------------------------------------------------------------- KDF --

@dataclass(frozen=True)
class KdfParams:
    algorithm: str = "argon2id"
    time_cost: int = 4
    memory_cost: int = 262144      # KiB  -> 256 MiB
    parallelism: int = 4
    hash_len: int = KEY_LEN

    def to_dict(self) -> dict:
        return {
            "algorithm": self.algorithm,
            "time_cost": self.time_cost,
            "memory_cost": self.memory_cost,
            "parallelism": self.parallelism,
            "hash_len": self.hash_len,
        }

    @staticmethod
    def from_dict(d: dict) -> "KdfParams":
        return KdfParams(
            algorithm=str(d["algorithm"]),
            time_cost=int(d["time_cost"]),
            memory_cost=int(d["memory_cost"]),
            parallelism=int(d["parallelism"]),
            hash_len=int(d["hash_len"]),
        )


def calibrate_kdf(target_seconds: float = 0.8) -> KdfParams:
    """Pick Argon2id parameters that take roughly ``target_seconds`` here.

    Memory is the parameter that actually hurts an attacker with custom
    hardware, so it is fixed high (256 MiB) and only the time cost is tuned.
    """
    memory = 262144
    parallelism = min(4, max(1, (os.cpu_count() or 2)))
    salt = random_bytes(SALT_LEN)

    for time_cost in (1, 2, 3, 4, 6, 8, 10):
        params = KdfParams(time_cost=time_cost, memory_cost=memory,
                           parallelism=parallelism)
        start = time.perf_counter()
        wipe(derive_kek("calibration-probe", salt, params))
        if time.perf_counter() - start >= target_seconds:
            return params
    return KdfParams(time_cost=10, memory_cost=memory, parallelism=parallelism)


def derive_kek(password: str, salt: bytes, params: KdfParams) -> bytearray:
    """Stretch ``password`` into a key-encryption key."""
    if params.algorithm != "argon2id":
        raise CryptoError("unsupported KDF: %s" % params.algorithm)
    raw = hash_secret_raw(
        secret=password.encode("utf-8"),
        salt=salt,
        time_cost=params.time_cost,
        memory_cost=params.memory_cost,
        parallelism=params.parallelism,
        hash_len=params.hash_len,
        type=Type.ID,
    )
    return bytearray(raw)


def subkey(master: bytes, info: bytes, length: int = KEY_LEN) -> bytearray:
    """Derive a purpose-bound sub-key from the master key."""
    hkdf = HKDF(algorithm=hashes.SHA256(), length=length, salt=None, info=info)
    return bytearray(hkdf.derive(bytes(master)))


# ------------------------------------------------------------- small buffers --

def aead_encrypt(key: bytes, plaintext: bytes, aad: bytes) -> bytes:
    """Encrypt a small in-memory buffer.  Returns ``nonce || ciphertext``."""
    nonce = random_bytes(NONCE_LEN)
    return nonce + AESGCM(bytes(key)).encrypt(nonce, plaintext, aad)


def aead_decrypt(key: bytes, blob: bytes, aad: bytes) -> bytes:
    if len(blob) < NONCE_LEN + TAG_LEN:
        raise CryptoError("ciphertext too short")
    nonce, ct = blob[:NONCE_LEN], blob[NONCE_LEN:]
    try:
        return AESGCM(bytes(key)).decrypt(nonce, ct, aad)
    except Exception as exc:                       # InvalidTag and friends
        raise CryptoError("authentication failed") from exc


# ------------------------------------------------------------ blob streaming --

def blob_aad_base(vault_id: bytes, blob_id: bytes) -> bytes:
    """Context that every chunk of one blob is bound to."""
    return BLOB_MAGIC + vault_id + blob_id + struct.pack(">I", CHUNK_SIZE)


def _chunk_aad(base: bytes, index: int, last: bool) -> bytes:
    return base + struct.pack(">QB", index, 1 if last else 0)


def _nonce(index: int) -> bytes:
    # The file key is random and used for exactly one stream, so a plain
    # counter nonce can never repeat under the same key.
    return index.to_bytes(NONCE_LEN, "big")


def _write_chunk(dst: BinaryIO, aes: AESGCM, base: bytes,
                 index: int, plaintext: bytes, last: bool) -> None:
    ct = aes.encrypt(_nonce(index), plaintext, _chunk_aad(base, index, last))
    dst.write(struct.pack(">I", len(ct)))
    dst.write(ct)


def encrypt_stream(src: BinaryIO, dst: BinaryIO, key: bytes, aad_base: bytes,
                   progress: ProgressCb = None, cancel=None):
    """Encrypt ``src`` into ``dst``.

    Returns ``(plaintext_size, sha256_hex)``.  The plaintext is zero-padded up
    to a multiple of ``PAD_BLOCK`` so that small files do not reveal their
    exact length; the true length lives in the encrypted index.
    """
    aes = AESGCM(bytes(key))
    dst.write(BLOB_MAGIC + struct.pack(">I", CHUNK_SIZE))

    digest = hashlib.sha256()
    index = 0
    size = 0

    current = src.read(CHUNK_SIZE)
    size += len(current)
    digest.update(current)

    while True:
        if cancel is not None and cancel.is_set():
            raise CryptoError("cancelled")
        nxt = src.read(CHUNK_SIZE)
        if nxt:
            _write_chunk(dst, aes, aad_base, index, current, last=False)
            index += 1
            current = nxt
            size += len(nxt)
            digest.update(nxt)
            if progress:
                progress(size)
        else:
            pad = (-size) % PAD_BLOCK
            _write_chunk(dst, aes, aad_base, index,
                         current + b"\x00" * pad, last=True)
            if progress:
                progress(size)
            break

    return size, digest.hexdigest()


def decrypt_stream(src: BinaryIO, dst: BinaryIO, key: bytes, aad_base: bytes,
                   plaintext_size: int, progress: ProgressCb = None,
                   cancel=None) -> str:
    """Decrypt ``src`` into ``dst``; returns the sha256 hex of the plaintext.

    A truncated blob is detected because the final chunk carries an
    end-of-stream flag in its associated data: cutting the file short makes an
    interior chunk look final and its tag check fails.
    """
    header = src.read(8)
    if len(header) != 8 or header[:4] != BLOB_MAGIC:
        raise CryptoError("not a vault blob")
    (chunk_size,) = struct.unpack(">I", header[4:8])
    if chunk_size != CHUNK_SIZE:
        raise CryptoError("unsupported chunk size")

    aes = AESGCM(bytes(key))
    digest = hashlib.sha256()
    written = 0
    index = 0

    length_field = src.read(4)
    if not length_field:
        raise CryptoError("empty blob")

    while length_field:
        if cancel is not None and cancel.is_set():
            raise CryptoError("cancelled")
        if len(length_field) != 4:
            raise CryptoError("truncated blob")
        (ct_len,) = struct.unpack(">I", length_field)
        if ct_len < TAG_LEN or ct_len > MAX_CHUNK_CT:
            raise CryptoError("implausible chunk length")
        ct = src.read(ct_len)
        if len(ct) != ct_len:
            raise CryptoError("truncated blob")

        nxt = src.read(4)
        last = not nxt
        try:
            pt = aes.decrypt(_nonce(index), ct,
                             _chunk_aad(aad_base, index, last))
        except Exception as exc:
            raise CryptoError(
                "authentication failed (corrupt or tampered)") from exc

        keep = max(0, min(len(pt), plaintext_size - written))
        if keep:
            view = pt[:keep]
            dst.write(view)
            digest.update(view)
            written += keep
        if progress:
            progress(written)

        length_field = nxt
        index += 1

    if written != plaintext_size:
        raise CryptoError("blob shorter than recorded size")
    return digest.hexdigest()
