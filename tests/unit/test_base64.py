"""Unit tests for strict base64 decoding of the direct flow (implementation guide §5.4, §6.1).

`decode_base64_strict` is the FIRST thing that touches a base64 upload
(`file.content_base64` in a JSON body). It must:
  * accept what real clients send (data-URL prefix, 76-column line wrapping),
  * reject anything that is not pure base64 with 400 `invalid_base64`,
  * reject anything larger than the inline limit with 413 BEFORE decoding,
  * never echo the submitted value in the error (NFR-S8).
"""

from __future__ import annotations

import base64

import pytest

from app.config import settings
from app.core.errors import ApiError
from app.modules.extraction.service import decode_base64_strict

PAYLOAD = b"%PDF-1.4 tiny synthetic file"
ENCODED = base64.b64encode(PAYLOAD).decode()


def test_plain_base64_decodes():
    assert decode_base64_strict(ENCODED) == PAYLOAD


def test_data_url_prefix_is_accepted():
    # Browsers produce this shape from FileReader.readAsDataURL().
    assert decode_base64_strict("data:application/pdf;base64," + ENCODED) == PAYLOAD


def test_newlines_and_spaces_are_ignored():
    # MIME-style encoders wrap every 76 characters; some add spaces too.
    wrapped = "\n".join(ENCODED[i:i + 8] for i in range(0, len(ENCODED), 8))
    assert decode_base64_strict(" " + wrapped + "\r\n") == PAYLOAD


@pytest.mark.parametrize("bad", [
    ENCODED[:-2] + "!!",       # characters outside the base64 alphabet
    ENCODED + "=",             # broken padding
    "not base64 at all ☺",
])
def test_invalid_characters_are_rejected(bad):
    with pytest.raises(ApiError) as exc:
        decode_base64_strict(bad)
    assert exc.value.status == 400
    assert exc.value.code == "invalid_base64"
    # NFR-S8: the error message must not contain any of the submitted value.
    assert bad not in exc.value.message


def test_non_string_is_rejected():
    # A JSON body could send a number or an object instead of a string.
    with pytest.raises(ApiError) as exc:
        decode_base64_strict(12345)        # type: ignore[arg-type]
    assert exc.value.code == "invalid_base64"


def test_too_big_is_413_before_decoding(monkeypatch):
    # Lower the limit so the test doesn't need to build a 4 MB string.
    monkeypatch.setattr(settings, "max_inline_bytes", 10)
    with pytest.raises(ApiError) as exc:
        # 40 base64 chars ~ 30 bytes > 10. Note the string is ALSO invalid ("!"):
        # the size check must run first, so we get 413, not 400.
        decode_base64_strict("A" * 39 + "!")
    assert exc.value.status == 413
    assert exc.value.code == "file_too_large"
