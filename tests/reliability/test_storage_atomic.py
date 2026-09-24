import os
import pytest
from app import mdstore


def test_frontmatter_preserves_extensions():
    meta = {"id": "stable", "title": "Example", "custom": {"a": [1, 2]}, "empty": []}
    loaded, _ = mdstore.parse_document(mdstore.render_document(meta, "body"))
    assert loaded == meta


def test_atomic_replace_failure_keeps_original(tmp_path, monkeypatch):
    path = tmp_path / "doc.md"
    path.write_text("old")
    def fail(*args):
        raise OSError("replace failed")
    monkeypatch.setattr(os, "replace", fail)
    assert hasattr(mdstore, "atomic_write")
    with pytest.raises(OSError):
        mdstore.atomic_write(path, "new")
    assert path.read_text() == "old"
    assert list(tmp_path.iterdir()) == [path]
