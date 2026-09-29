"""Shamir secret sharing over GF(2^8), and a human-writable share encoding.

A recovery secret is split into ``count`` shares of which any ``threshold``
reconstruct it.  Fewer than ``threshold`` shares reveal *nothing* about the
secret -- that is a property of the maths, not of how hard the shares are to
guess: with one share of a 2-of-3 split, every possible secret remains exactly
as likely as it was before.

The field is GF(2^8) with the AES reduction polynomial 0x11B, so every byte of
the secret is split independently.  Shares are the same length as the secret.

Encoding uses Crockford Base32, which drops I, L, O and U so a handwritten
share cannot be misread, and carries a checksum plus a short vault
fingerprint so a typo or a share from a different vault is caught before
anything is decrypted.
"""

from __future__ import annotations

import hashlib
import secrets

# ------------------------------------------------------------ GF(2^8) maths --

_EXP = [0] * 512
_LOG = [0] * 256


def _build_tables() -> None:
    x = 1
    for i in range(255):
        _EXP[i] = x
        _LOG[x] = i
        # multiply by the generator 3: x*2 ^ x, reduced by 0x11B
        doubled = x << 1
        if doubled & 0x100:
            doubled ^= 0x11B
        x = (doubled ^ x) & 0xFF
    for i in range(255, 512):
        _EXP[i] = _EXP[i - 255]


_build_tables()


def _mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return _EXP[_LOG[a] + _LOG[b]]


def _div(a: int, b: int) -> int:
    if b == 0:
        raise ZeroDivisionError("division by zero in GF(256)")
    if a == 0:
        return 0
    return _EXP[(_LOG[a] - _LOG[b]) % 255]


# ------------------------------------------------------------------ sharing --

MAX_SHARES = 15          # the share index is packed into half a byte
MIN_THRESHOLD = 2


def split(secret: bytes, threshold: int, count: int) -> list:
    """Split ``secret`` into ``count`` shares, any ``threshold`` of which combine.

    Returns a list of ``(index, share_bytes)`` with indices 1..count.
    """
    if not secret:
        raise ValueError("nothing to split")
    if not MIN_THRESHOLD <= threshold <= count:
        raise ValueError("threshold must be between %d and the share count"
                         % MIN_THRESHOLD)
    if count > MAX_SHARES:
        raise ValueError("at most %d shares" % MAX_SHARES)

    shares = [bytearray() for _ in range(count)]
    for byte in secret:
        # A random polynomial whose constant term is this byte of the secret.
        coefficients = [byte] + [secrets.randbelow(256)
                                 for _ in range(threshold - 1)]
        for index in range(1, count + 1):
            acc = 0
            for coefficient in reversed(coefficients):       # Horner
                acc = _mul(acc, index) ^ coefficient
            shares[index - 1].append(acc)
    return [(i + 1, bytes(s)) for i, s in enumerate(shares)]


def combine(points: list) -> bytes:
    """Reconstruct the secret from ``(index, share_bytes)`` pairs."""
    if not points:
        raise ValueError("no shares given")
    indices = [x for x, _ in points]
    if len(set(indices)) != len(indices):
        raise ValueError("the same share was given more than once")
    if any(x < 1 or x > 255 for x in indices):
        raise ValueError("share index out of range")
    length = len(points[0][1])
    if any(len(data) != length for _x, data in points):
        raise ValueError("shares are not all the same length")

    out = bytearray(length)
    for position in range(length):
        acc = 0
        for i, (xi, data) in enumerate(points):
            # Lagrange basis polynomial for this point, evaluated at x = 0.
            numerator, denominator = 1, 1
            for j, (xj, _d) in enumerate(points):
                if i == j:
                    continue
                numerator = _mul(numerator, xj)
                denominator = _mul(denominator, xi ^ xj)
            acc ^= _mul(data[position], _div(numerator, denominator))
        out[position] = acc
    return bytes(out)


# ----------------------------------------------------------- share encoding --

ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"        # Crockford: no I L O U
_DECODE = {c: i for i, c in enumerate(ALPHABET)}
_DECODE.update({"I": 1, "L": 1, "O": 0, "U": 0})     # common misreadings
SHARE_VERSION = 1
CHECKSUM_CONTEXT = b"desktopvault/share/v1"
GROUP = 4


class ShareError(Exception):
    """A share that is malformed, mistyped, or from another vault."""


def _b32_encode(raw: bytes) -> str:
    bits = int.from_bytes(raw, "big")
    width = (len(raw) * 8 + 4) // 5
    chars = []
    for i in range(width - 1, -1, -1):
        chars.append(ALPHABET[(bits >> (i * 5)) & 0x1F])
    return "".join(chars)


def _b32_decode(text: str, size: int) -> bytes:
    bits = 0
    for char in text:
        try:
            bits = (bits << 5) | _DECODE[char]
        except KeyError:
            raise ShareError("%r is not a valid character in a share" % char)
    return bits.to_bytes((len(text) * 5 + 7) // 8, "big")[-size:]


def fingerprint(vault_id: bytes) -> bytes:
    return hashlib.sha256(b"desktopvault/vault/" + vault_id).digest()[:2]


def encode_share(index: int, data: bytes, threshold: int,
                 vault_id: bytes) -> str:
    """Render one share as grouped Crockford Base32 with a checksum."""
    if not 1 <= index <= MAX_SHARES:
        raise ShareError("share index out of range")
    if not MIN_THRESHOLD <= threshold <= MAX_SHARES:
        raise ShareError("threshold out of range")
    body = (bytes([SHARE_VERSION, (threshold << 4) | index])
            + fingerprint(vault_id) + data)
    digest = hashlib.sha256(CHECKSUM_CONTEXT + body).digest()[:4]
    text = _b32_encode(body + digest)
    return "-".join(text[i:i + GROUP] for i in range(0, len(text), GROUP))


def decode_share(text: str, vault_id: bytes = None) -> tuple:
    """Parse a share back to ``(index, data, threshold)``.

    Raises :class:`ShareError` with something a person can act on.
    """
    cleaned = "".join(ch for ch in (text or "").upper()
                      if ch.isalnum())
    if not cleaned:
        raise ShareError("no share was entered")
    # 2 header + 2 fingerprint + N secret + 4 checksum
    raw_len = (len(cleaned) * 5) // 8
    if raw_len < 9:
        raise ShareError("this share is too short to be complete")
    raw = _b32_decode(cleaned, raw_len)

    body, digest = raw[:-4], raw[-4:]
    if hashlib.sha256(CHECKSUM_CONTEXT + body).digest()[:4] != digest:
        raise ShareError(
            "this share did not pass its checksum - it is probably mistyped")
    if body[0] != SHARE_VERSION:
        raise ShareError("this share was made by a different version")
    threshold, index = body[1] >> 4, body[1] & 0x0F
    if index < 1 or threshold < MIN_THRESHOLD:
        raise ShareError("this share is malformed")
    if vault_id is not None and body[2:4] != fingerprint(vault_id):
        raise ShareError("this share belongs to a different vault")
    return index, body[4:], threshold
