"""Common wire-shape models — camelCase on the wire, cursor pagination."""

from typing import TypeVar

from pydantic import BaseModel, ConfigDict

# A ULID-valued string identifier (e.g. ``rp_<ulid>``).  Stored and serialised
# as a plain ``str``; the alias documents the expected shape.
type UlidStr = str


def _to_camel(name: str) -> str:
    """Convert snake_case to camelCase for JSON serialisation."""
    parts = name.split("_")
    return parts[0] + "".join(p.capitalize() for p in parts[1:])


class CamelModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=_to_camel,
        populate_by_name=True,
        from_attributes=True,
        extra="forbid",
    )


T = TypeVar("T")


class CursorPage[T](CamelModel):
    items: list[T]
    next_cursor: str | None = None
    total: int | None = None
