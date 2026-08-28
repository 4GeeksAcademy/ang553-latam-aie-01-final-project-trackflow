"""Pytest fixtures for isolated auth TinyDB tests."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pytest
import tinydb as tinydb_module
from tinydb import TinyDB
from tinydb.table import Table

# Required by services.api.auth_settings during module import.
os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret-key")

_REPO_ROOT = Path(__file__).resolve().parents[1]
_REAL_AUTH_DB_PATH = (_REPO_ROOT / "services/api/data/auth.json").resolve()
_IMPORT_GUARD_DIR = Path(tempfile.mkdtemp(prefix="trackflow-auth-import-"))
_IMPORT_GUARD_DB_PATH = (_IMPORT_GUARD_DIR / "auth-import-guard.json").resolve()

_ORIGINAL_TINYDB = tinydb_module.TinyDB
_REAL_PATH_REDIRECT_HITS = 0


def _guarded_tinydb(*args, **kwargs):
    """Redirect real auth.json access during module import to a temp file."""
    global _REAL_PATH_REDIRECT_HITS

    if args:
        db_target = Path(str(args[0])).resolve()
        if db_target == _REAL_AUTH_DB_PATH:
            _REAL_PATH_REDIRECT_HITS += 1
            new_args = list(args)
            new_args[0] = str(_IMPORT_GUARD_DB_PATH)
            return _ORIGINAL_TINYDB(*new_args, **kwargs)
    elif "path" in kwargs:
        db_target = Path(str(kwargs["path"])).resolve()
        if db_target == _REAL_AUTH_DB_PATH:
            _REAL_PATH_REDIRECT_HITS += 1
            new_kwargs = dict(kwargs)
            new_kwargs["path"] = str(_IMPORT_GUARD_DB_PATH)
            return _ORIGINAL_TINYDB(*args, **new_kwargs)

    return _ORIGINAL_TINYDB(*args, **kwargs)


# Install import-time guard before importing auth modules.
tinydb_module.TinyDB = _guarded_tinydb

from services.api import auth_database, auth_security, auth_services

# Restore TinyDB constructor right after auth modules are loaded.
tinydb_module.TinyDB = _ORIGINAL_TINYDB


@dataclass(frozen=True)
class IsolatedAuthDB:
    """References to the temporary TinyDB and auth tables used in tests."""

    db_path: Path
    real_db_path: Path
    import_guard_db_path: Path
    real_path_redirect_hits: int
    auth_db: TinyDB
    users: Table
    profiles: Table
    password_reset_tokens: Table


@pytest.fixture(scope="function")
def isolated_auth_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> IsolatedAuthDB:
    """Create and patch a per-test TinyDB for auth tables.

    The fixture replaces module-level table references where they are actually
    used so auth tests cannot write to services/api/data/auth.json.
    """
    real_db_path = Path(auth_database._AUTH_DB_PATH).resolve()  # noqa: SLF001
    temp_db_path = (tmp_path / "auth-test.json").resolve()

    if temp_db_path == real_db_path:
        raise RuntimeError("Unsafe test setup: temporary DB path matches real auth.json")

    real_bytes_before = real_db_path.read_bytes() if real_db_path.exists() else None

    temp_auth_db = TinyDB(str(temp_db_path))
    temp_users = temp_auth_db.table("users")
    temp_profiles = temp_auth_db.table("profiles")
    temp_password_reset_tokens = temp_auth_db.table("password_reset_tokens")

    # Patch definition module references.
    monkeypatch.setattr(auth_database, "auth_db", temp_auth_db)
    monkeypatch.setattr(auth_database, "users", temp_users)
    monkeypatch.setattr(auth_database, "profiles", temp_profiles)
    monkeypatch.setattr(auth_database, "password_reset_tokens", temp_password_reset_tokens)

    # Patch copied imports in service/security modules.
    monkeypatch.setattr(auth_services, "users", temp_users)
    monkeypatch.setattr(auth_services, "profiles", temp_profiles)
    monkeypatch.setattr(auth_services, "password_reset_tokens", temp_password_reset_tokens)
    monkeypatch.setattr(auth_security, "_users_db", temp_users)

    state = IsolatedAuthDB(
        db_path=temp_db_path,
        real_db_path=real_db_path,
        import_guard_db_path=_IMPORT_GUARD_DB_PATH,
        real_path_redirect_hits=_REAL_PATH_REDIRECT_HITS,
        auth_db=temp_auth_db,
        users=temp_users,
        profiles=temp_profiles,
        password_reset_tokens=temp_password_reset_tokens,
    )

    yield state

    temp_auth_db.close()

    real_bytes_after = real_db_path.read_bytes() if real_db_path.exists() else None
    if real_bytes_before != real_bytes_after:
        raise AssertionError(
            "Real auth.json changed during tests. Fixtures must use only temporary TinyDB files."
        )


@pytest.fixture(scope="function", autouse=True)
def _auto_isolate_auth_db(isolated_auth_db: IsolatedAuthDB) -> IsolatedAuthDB:
    """Apply TinyDB isolation automatically for every test."""
    return isolated_auth_db
