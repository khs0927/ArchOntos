from __future__ import annotations

from dataclasses import dataclass, field
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker


@dataclass(slots=True)
class HyperedgeMember:
    role: str
    ref_type: str
    ref_id: str
    ordinal: int = 0


@dataclass(slots=True)
class Hyperedge:
    id: str
    hyperedge_type: str
    properties: dict
    members: list[HyperedgeMember] = field(default_factory=list)

    def as_api(self) -> dict:
        return {
            "id": self.id,
            "hyperedge_type": self.hyperedge_type,
            "properties": self.properties,
            "members": [
                {
                    "role": member.role,
                    "ref_type": member.ref_type,
                    "ref_id": member.ref_id,
                    "ordinal": member.ordinal,
                }
                for member in self.members
            ],
        }


class MemoryHyperedgeStore:
    def __init__(self) -> None:
        self._rows: dict[str, Hyperedge] = {}

    async def create(
        self,
        hyperedge_type: str,
        members: list[HyperedgeMember],
        properties: dict | None = None,
    ) -> Hyperedge:
        row = Hyperedge(
            id=str(uuid4()),
            hyperedge_type=hyperedge_type,
            properties=properties or {},
            members=list(members),
        )
        self._rows[row.id] = row
        return row

    async def get(self, hyperedge_id: str) -> Hyperedge | None:
        return self._rows.get(hyperedge_id)


class PostgresHyperedgeStore:
    def __init__(self, session_factory: async_sessionmaker):
        self.session_factory = session_factory

    async def create(
        self,
        hyperedge_type: str,
        members: list[HyperedgeMember],
        properties: dict | None = None,
    ) -> Hyperedge:
        import json

        async with self.session_factory() as session:
            async with session.begin():
                created = await session.execute(
                    text(
                        """
                        INSERT INTO hyperedge(hyperedge_type, properties_json)
                        VALUES (:kind, CAST(:properties AS jsonb))
                        RETURNING id::text
                        """
                    ),
                    {
                        "kind": hyperedge_type,
                        "properties": json.dumps(properties or {}, sort_keys=True),
                    },
                )
                hyperedge_id = created.scalar_one()
                for member in members:
                    await session.execute(
                        text(
                            """
                            INSERT INTO hyperedge_member(hyperedge_id, role, ref_type, ref_id, ordinal)
                            VALUES (CAST(:id AS uuid), :role, :ref_type, :ref_id, :ordinal)
                            ON CONFLICT (hyperedge_id, role, ref_type, ref_id) DO NOTHING
                            """
                        ),
                        {
                            "id": hyperedge_id,
                            "role": member.role,
                            "ref_type": member.ref_type,
                            "ref_id": member.ref_id,
                            "ordinal": member.ordinal,
                        },
                    )
        loaded = await self.get(hyperedge_id)
        assert loaded is not None
        return loaded

    async def get(self, hyperedge_id: str) -> Hyperedge | None:
        async with self.session_factory() as session:
            head = (
                await session.execute(
                    text(
                        """
                        SELECT id::text, hyperedge_type, properties_json
                        FROM hyperedge WHERE id = CAST(:id AS uuid)
                        """
                    ),
                    {"id": hyperedge_id},
                )
            ).first()
            if head is None:
                return None
            members = (
                await session.execute(
                    text(
                        """
                        SELECT role, ref_type, ref_id, ordinal
                        FROM hyperedge_member
                        WHERE hyperedge_id = CAST(:id AS uuid)
                        ORDER BY ordinal, role
                        """
                    ),
                    {"id": hyperedge_id},
                )
            ).all()
        return Hyperedge(
            id=head.id,
            hyperedge_type=head.hyperedge_type,
            properties=dict(head.properties_json),
            members=[
                HyperedgeMember(role=item.role, ref_type=item.ref_type, ref_id=item.ref_id, ordinal=item.ordinal)
                for item in members
            ],
        )
