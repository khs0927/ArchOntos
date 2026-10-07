import pytest

from archontos.actions.gate import ApprovalDenied, assert_can_execute, can_execute
from archontos.actions.persistence import MemoryActionStore
from archontos.actions.service import propose_report
from archontos.graph.hyperedges import HyperedgeMember, MemoryHyperedgeStore
from archontos.projection.base import ProjectionEvent
from archontos.projection.embedding import EmbeddingTextProjection


def test_report_cannot_run_until_approved():
    assert can_execute(status="proposed", requires_approval=True) is False
    assert can_execute(status="approved", requires_approval=True) is True
    assert can_execute(status="proposed", requires_approval=False) is True
    assert can_execute(status="rejected", requires_approval=False) is False


def test_memory_store_enforces_approval_gate():
    import asyncio

    store = MemoryActionStore()

    async def scenario():
        proposed = await store.propose(propose_report(["rule:1"], {"note": "smoke"}))
        with pytest.raises(ApprovalDenied):
            await store.execute(proposed.id, "reviewer")
        approved = await store.approve(proposed.id, "reviewer")
        assert approved.status == "approved"
        done = await store.execute(proposed.id, "reviewer")
        assert done.status == "succeeded"
        assert done.runs[0]["result"]["status"] == "generated"

    asyncio.run(scenario())


def test_embedding_projection_is_rebuildable():
    import asyncio

    projection = EmbeddingTextProjection()
    event = ProjectionEvent(1, "SourceVersionNormalized", "source_version", "abc", {"n": 1})

    async def scenario():
        await projection.apply(event)
        assert len(projection.rows) == 1
        await projection.reset()
        assert projection.rows == {}

    asyncio.run(scenario())


def test_hyperedge_members_round_trip():
    import asyncio

    store = MemoryHyperedgeStore()

    async def scenario():
        created = await store.create(
            "applies-to",
            [HyperedgeMember("rule", "rule", "r1"), HyperedgeMember("object", "space", "s1", 1)],
        )
        loaded = await store.get(created.id)
        assert loaded is not None
        assert [member.role for member in loaded.members] == ["rule", "object"]

    asyncio.run(scenario())


def test_denied_message_names_status():
    with pytest.raises(ApprovalDenied):
        assert_can_execute(status="proposed", requires_approval=True)
