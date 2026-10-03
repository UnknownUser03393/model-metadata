"""Source acquisition, encoding detection, and document shape validation.

Two things are worth keeping from the previous iteration. First, encoding is sniffed from
the bytes rather than guessed by ``open(encoding=...)``: a wrong guess decodes to mojibake
instead of raising, and the earlier snapshot in this repo really was UTF-16LE while the
upstream was UTF-8. Second, the document shape is asserted up front.

That second point matters more than it looks. The previous dataset was a flat
``model_string -> record`` map; this one is a wrapper document whose models live in a
``models`` **list**. Feeding this document to a loader that assumes the old shape would not
crash -- it would silently ingest the ten top-level metadata keys as ten "models" and drop
all 651 real ones. The assertion below is what makes that a loud failure.
"""

import hashlib
import json

SNAPSHOT_DIR = "snapshot"


class SnapshotError(ValueError):
    """The document is not the shape this loader can mirror."""


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


def load_document(raw_bytes):
    """Decode and validate. Returns the parsed document."""
    try:
        doc = json.loads(decode(raw_bytes))
    except UnicodeDecodeError as exc:
        raise SnapshotError(f"could not decode snapshot: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SnapshotError(f"snapshot is not valid JSON: {exc}") from exc
    return validate_document(doc)


def validate_document(doc):
    if not isinstance(doc, dict):
        raise SnapshotError(
            f"top level must be an object, got {type(doc).__name__}"
        )
    models = doc.get("models")
    if models is None:
        raise SnapshotError(
            "no 'models' key. This looks like a flat model map; that is the *previous* "
            "dataset format, not the curated snapshot format."
        )
    if not isinstance(models, list):
        raise SnapshotError(
            f"'models' must be a list, got {type(models).__name__}"
        )
    for i, m in enumerate(models):
        if not isinstance(m, dict):
            raise SnapshotError(f"models[{i}] is {type(m).__name__}, expected object")
        if not m.get("catalog_id"):
            raise SnapshotError(f"models[{i}] has no catalog_id")
    return doc


def read_snapshot(path):
    """Return (document, file_sha256)."""
    with open(path, "rb") as fh:
        raw = fh.read()
    return load_document(raw), hashlib.sha256(raw).hexdigest()
