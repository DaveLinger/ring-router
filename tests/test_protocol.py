import time

import pytest

from ringrouter import protocol
from ringrouter.fm import checked_rewrite


def test_roundtrip_verify_and_parse():
    h, b = protocol.build_request("Tell Terri hi", "s3cret", include_audio=True)
    v = protocol.verify(h, b, "s3cret")
    assert v.trigger == "single-click-hold" and not v.is_test
    fields = protocol.parse_multipart(h["Content-Type"], b)
    assert fields["transcription"] == "Tell Terri hi" and "audio" not in fields and fields["client"] == "ring"


def test_signature_covers_trigger():
    h, b = protocol.build_request("x", "s3cret")
    h["X-Index-Trigger"] = "double-click-hold"
    with pytest.raises(protocol.RejectError) as e:
        protocol.verify(h, b, "s3cret")
    assert e.value.status == 401


def test_signature_matches_reference_format():
    # v1\n{ts}\n{delivery}\n{trigger}\n{test}\n + body, HMAC-SHA256 hex
    import hashlib
    import hmac
    sig = protocol.sign("k", "100", "abc", "single-click-hold", False, b"BODY")
    assert sig == hmac.new(b"k", b"v1\n100\nabc\nsingle-click-hold\n0\nBODY", hashlib.sha256).hexdigest()


def test_stale_timestamp():
    h, b = protocol.build_request("x", "k", ts=int(time.time()) - 301)
    with pytest.raises(protocol.RejectError):
        protocol.verify(h, b, "k")


@pytest.mark.parametrize("orig,out,expect_used", [
    ("when she'll be home", "When will you be home?", True),
    ("when she'll be home", "", False),
    ("when she'll be home", "When will Terri be home?", False),          # talks about the recipient
    ("x", "y" * 200, False),                                            # far too long
    ("if they want beers", "you want beers Friday?", True),             # capitalised
    ("if the report is done", "Terri: Is the report done?", True),      # leading name stripped
])
def test_checked_rewrite(orig, out, expect_used):
    text, used = checked_rewrite(orig, out, ["Terri", "terry"])
    assert used == expect_used
    if used:
        assert text[0].isupper()
