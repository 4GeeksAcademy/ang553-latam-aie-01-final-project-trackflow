"""Smoke tests for pytest infrastructure isolation."""

from __future__ import annotations

from tinydb import Query

from services.api.auth_models import UserCreate
from services.api.auth_services import create_user


def test_auth_tinydb_is_isolated_from_real_file(isolated_auth_db) -> None:
    """Validate writes go to the temporary TinyDB and not to auth.json."""
    assert isolated_auth_db.db_path != isolated_auth_db.real_db_path
    assert isolated_auth_db.import_guard_db_path != isolated_auth_db.real_db_path
    assert isolated_auth_db.real_path_redirect_hits >= 1

    assert isolated_auth_db.users.all() == []
    assert isolated_auth_db.profiles.all() == []
    assert isolated_auth_db.password_reset_tokens.all() == []

    created = create_user(
        UserCreate(email="fixture-smoke@example.com", password="TestPass123")
    )

    q = Query()
    stored_docs = isolated_auth_db.users.search(q.id == created.id)

    assert len(stored_docs) == 1
    assert stored_docs[0]["email"] == "fixture-smoke@example.com"
