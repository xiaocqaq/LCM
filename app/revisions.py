"""Deterministic CAS tokens for the complete user-editable document state.

REST: read ``revision``, PATCH with ``expectedRevision``.
MCP: read ``revision``, memory_update with ``expected_revision``.
Legacy contentHash/expectedHash (MCP content_hash/expected_hash) remain
body-only checks: they cannot detect concurrent metadata edits. Omitted CAS
parameters retain unconditional-update semantics. Branch saves are separate
Git writes, not updates to the current document, and do not use this CAS.
Access counters/timestamps and derived relationship state are not editable and
must not invalidate a token. This is a state hash, not a monotonic edit counter.
"""
import hashlib
import json

from .models import Document


_EDITABLE_FIELDS = (
    'title', 'library', 'md_type', 'project', 'tags', 'importance',
    'source', 'links', 'content',
)


def document_revision(doc: Document) -> str:
    state = {field: getattr(doc, field, None) for field in _EDITABLE_FIELDS}
    encoded = json.dumps(state, sort_keys=True, ensure_ascii=False,
                         separators=(',', ':')).encode('utf-8')
    return 'v1:' + hashlib.sha256(encoded).hexdigest()
