"""Unique ids that sort by creation time.

We use ULIDs: 26 characters, the first 10 encode the creation time in milliseconds and the
last 16 are random. Example: "job_01k7f3w2g5x8c9v0b1n2m3q4r5".

Why not a plain random UUID? Sorting ULIDs as text also sorts them by time, so lists of
jobs come out in creation order for free (folder names in data/ sort by time).
A prefix (job_) makes it obvious what kind of id you are looking at.
"""

import os
import time

# Crockford's base32 alphabet: no I, L, O, U, so ids are hard to misread.
_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"


def ulid() -> str:
    """48-bit millisecond timestamp + 80 random bits, written as 26 base32 characters."""
    value = (int(time.time() * 1000) << 80) | int.from_bytes(os.urandom(10), "big")
    chars = []
    for _ in range(26):  # 26 characters x 5 bits = 130 bits, enough for 128
        chars.append(_ALPHABET[value & 31])
        value >>= 5
    return "".join(reversed(chars))


def new_id(prefix: str) -> str:
    """new_id("job") -> "job_01k7f3w2g5x8c9v0b1n2m3q4r5"."""
    return f"{prefix}_{ulid()}"
