"""Source acquisition and encoding detection.

The upstream file at BerriAI/litellm is UTF-8, but a local snapshot in this repo has
historically been UTF-16LE with a BOM. Decoding cannot be left to ``open(encoding=...)``
guessing: a wrong guess (cp1252) silently mangles non-ASCII model names instead of
raising, so the bytes are sniffed explicitly.
"""

import hashlib
import json
import urllib.request

UPSTREAM_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json"
)


def decode(raw_bytes):
    """Decode source bytes to str, sniffing the encoding from BOM / first bytes."""
    if raw_bytes[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw_bytes.decode("utf-16")
    if raw_bytes[:3] == b"\xef\xbb\xbf":
        return raw_bytes.decode("utf-8-sig")
    # No BOM: UTF-16LE JSON almost always opens with '{' NUL -> 0x7B 0x00.
    if len(raw_bytes) > 1 and raw_bytes[0] == 0x7B and raw_bytes[1] == 0x00:
        return raw_bytes.decode("utf-16-le")
    return raw_bytes.decode("utf-8")


def read_source(path=None, url=None, timeout=60):
    """Return (records, sha256_hex, origin_label)."""
    if path:
        with open(path, "rb") as fh:
            raw = fh.read()
        origin = path
    else:
        with urllib.request.urlopen(url or UPSTREAM_URL, timeout=timeout) as resp:
            raw = resp.read()
        origin = url or UPSTREAM_URL

    digest = hashlib.sha256(raw).hexdigest()
    records = json.loads(decode(raw))
    if not isinstance(records, dict):
        raise ValueError(f"expected a JSON object at the top level, got {type(records).__name__}")
    return records, digest, origin
