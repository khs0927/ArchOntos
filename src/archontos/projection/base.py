from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class ProjectionEvent:
    sequence: int
    event_type: str
    aggregate_type: str
    aggregate_id: str
    payload: dict[str, Any]


class Projection(ABC):
    name: str

    @abstractmethod
    async def apply(self, event: ProjectionEvent) -> None:
        raise NotImplementedError


class RebuildableProjection(Projection):
    @abstractmethod
    async def reset(self) -> None:
        raise NotImplementedError
