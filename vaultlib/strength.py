"""A small offline password-strength estimator.

This is a heuristic, not a guarantee.  It rewards length far more than it
rewards punctuation, because against an offline attacker holding the vault file
length is what actually buys time.
"""

from __future__ import annotations

import math
import re

# Passwords and stems that appear at the top of every leak corpus.
COMMON = {
    "password", "passwd", "123456", "12345678", "123456789", "qwerty",
    "abc123", "letmein", "monkey", "dragon", "111111", "iloveyou", "admin",
    "welcome", "login", "master", "hello", "freedom", "whatever", "trustno1",
    "sunshine", "princess", "football", "baseball", "superman", "batman",
    "shadow", "michael", "jennifer", "jordan", "harley", "ranger", "hunter",
    "buster", "soccer", "hockey", "killer", "george", "andrew", "charlie",
    "secret", "summer", "winter", "spring", "autumn", "qwertyuiop", "asdfgh",
    "zxcvbn", "1q2w3e4r", "changeme", "default", "guest", "root", "toor",
}

KEYBOARD_RUNS = ("qwertyuiop", "asdfghjkl", "zxcvbnm", "1234567890")

LABELS = [
    ("Very weak", "#E5484D"),
    ("Weak",      "#E5484D"),
    ("Fair",      "#E2A03F"),
    ("Strong",    "#35B37E"),
    ("Excellent", "#35B37E"),
]


def _charset_size(pw: str) -> int:
    size = 0
    if re.search(r"[a-z]", pw):
        size += 26
    if re.search(r"[A-Z]", pw):
        size += 26
    if re.search(r"[0-9]", pw):
        size += 10
    if re.search(r"[ !-/:-@\[-`{-~]", pw):
        size += 32
    if re.search(r"[^\x00-\x7f]", pw):
        size += 100
    return max(size, 1)


def _has_run(pw: str, minimum: int = 4) -> bool:
    low = pw.lower()
    for run in KEYBOARD_RUNS:
        for i in range(len(run) - minimum + 1):
            piece = run[i:i + minimum]
            if piece in low or piece[::-1] in low:
                return True
    # ascending or descending character sequences, e.g. "abcd" / "4321"
    streak = 1
    for a, b in zip(low, low[1:]):
        if ord(b) - ord(a) in (1, -1):
            streak += 1
            if streak >= minimum:
                return True
        else:
            streak = 1
    return False


def estimate(pw: str) -> dict:
    """Return ``{bits, score (0-4), label, colour, hint}``."""
    if not pw:
        return {"bits": 0.0, "score": 0, "label": "", "colour": "#5C6478",
                "hint": ""}

    bits = len(pw) * math.log2(_charset_size(pw))
    hints = []
    low = pw.lower()

    stripped = re.sub(r"[^a-z]", "", low)
    if low in COMMON or stripped in COMMON:
        bits = min(bits, 8.0)
        hints.append("this is one of the most-guessed passwords there is")
    else:
        for word in COMMON:
            if len(word) >= 5 and word in low:
                bits -= 12
                hints.append("contains the common word %r" % word)
                break

    # Repetition and low variety cut the real search space.
    # A long passphrase naturally reuses letters, so only flag repetition when
    # the variety is genuinely low rather than merely below half the length.
    unique = len(set(pw))
    if unique <= 2 and len(pw) > 3:
        bits *= 0.35
        hints.append("almost all the same character")
    elif unique <= 5 and len(pw) > 8:
        bits *= 0.6
        hints.append("only a handful of distinct characters")
    elif len(pw) <= 16 and unique < len(pw) / 2:
        bits *= 0.8
        hints.append("a lot of repetition")

    if _has_run(pw):
        bits -= 10
        hints.append("contains a keyboard or counting sequence")

    if re.fullmatch(r"\d+", pw):
        bits -= 8
        hints.append("digits only")

    bits = max(0.0, bits)

    if bits < 36:
        score = 0
    elif bits < 55:
        score = 1
    elif bits < 75:
        score = 2
    elif bits < 100:
        score = 3
    else:
        score = 4

    if score <= 1 and len(pw) < 14 and not hints:
        hints.append("length is the thing that helps most - aim for 5+ words")

    label, colour = LABELS[score]
    return {"bits": bits, "score": score, "label": label, "colour": colour,
            "hint": hints[0] if hints else ""}
