"""Canonical inventory client registry.

This is a temporary domain boundary until a persisted Client entity exists.
The identifiers are opaque, fixed UUIDv4 values and must never be derived from
client names at runtime.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CanonicalClient:
    """A canonical inventory client identity and its display name."""

    client_id: str
    client_name: str


CANONICAL_CLIENTS: tuple[CanonicalClient, ...] = (
    CanonicalClient("7f6e3d21-8c4a-4b92-a1d7-5e9f2c6b1048", "PureStep Footwear"),
    CanonicalClient("2a9b7c41-6d8e-4f03-b5c2-9a1e7d4f8260", "SoundWave Electronics"),
    CanonicalClient("c4d8e2f7-1a63-4b90-8e25-6f3c9d7a1052", "GlowLab Cosmetics"),
    CanonicalClient("9e1b5d73-4c28-4a06-bf91-7d2e8c6a4305", "UrbanThread"),
)

CLIENTS_BY_ID = {client.client_id: client for client in CANONICAL_CLIENTS}
CLIENT_IDS_BY_NAME = {
    client.client_name: client.client_id for client in CANONICAL_CLIENTS
}


def get_canonical_client(client_id: str) -> CanonicalClient | None:
    """Return the canonical client for an ID, or ``None`` if unknown."""
    return CLIENTS_BY_ID.get(client_id)


def validate_client_pair(client_id: str, client_name: str) -> None:
    """Validate an authoritative ID and its matching display name."""
    client = get_canonical_client(client_id)
    if client is None:
        raise ValueError("Unknown client_id.")
    if client.client_name != client_name:
        raise ValueError("client_id and client_name do not match.")
