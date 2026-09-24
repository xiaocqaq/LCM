import asyncio
import json
import math
from unittest.mock import AsyncMock
from types import SimpleNamespace
import pytest
from app import service
from test_storage_transactions import Rows, Session, make_doc


def bootstrap_docs():
    docs = []
    for idx, kind in enumerate(['project_summary', 'project_summary', 'decision', 'preference', 'howto', 'fact']):
        doc = make_doc()
        doc.id = idx + 1
        doc.md_type = kind
        doc.title = kind
        doc.content = '丰富的正文需要有代表性并公平分配预算。' * 1000
        docs.append(doc)
    return docs


class BootSession(Session):
    async def execute(self, *a, **kw): return Rows(bootstrap_docs())


def test_bootstrap_includes_core_types_and_budgets_entire_payload(monkeypatch):
    monkeypatch.setattr(service.links_mod, 'expand_by_links', AsyncMock(return_value=[]))
    result = asyncio.run(service.bootstrap_context(BootSession(), SimpleNamespace(id=1), None, 2500))
    assert {'project_summary', 'decision', 'preference'} <= {d['type'] for d in result['documents']}
    actual_estimate = math.ceil(len(json.dumps(result, ensure_ascii=False)) * .85)
    assert actual_estimate <= result['token_budget']
    assert result['estimated_tokens'] == actual_estimate
    assert result['token_estimate_method'] == 'heuristic-json-chars-v1'


def test_bootstrap_budget_sweep(monkeypatch):
    monkeypatch.setattr(service.links_mod, 'expand_by_links', AsyncMock(return_value=[]))
    results = [asyncio.run(service.bootstrap_context(BootSession(), SimpleNamespace(id=1), None, b))
               for b in [0, 400, 1000, 2500, 5000, 15000, 100000]]
    costs = []
    counts = []
    for result in results:
        cost = math.ceil(len(json.dumps(result, ensure_ascii=False)) * .85)
        assert cost <= result['token_budget']
        assert result['estimated_tokens'] == cost
        costs.append(cost)
        counts.append(result['document_count'])
    assert costs == sorted(costs)
    assert counts == sorted(counts)
    assert len(set(costs)) > 3
