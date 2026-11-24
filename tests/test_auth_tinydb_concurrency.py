"""Reproduce and prevent concurrent JSONStorage access to the auth handle."""

from concurrent.futures import ThreadPoolExecutor
from json import JSONDecodeError
from threading import Event, current_thread

import pytest
from tinydb import storages
from tinydb.storages import JSONStorage

from services.api import auth_security, auth_services
from services.api.auth_models import UserCreate


def test_live_jsonstorage_handle_needs_the_shared_auth_lock(isolated_auth_db, monkeypatch):
    db = isolated_auth_db.auth_db
    storage = db.storage
    assert isinstance(storage, JSONStorage)
    isolated_auth_db.users.insert({
        "id": "qa-concurrency", "email": "concurrency@example.test",
        "hashed_password": "unused", "role": "user", "is_active": True,
    })
    real_load = storages.json.load
    first_in_load = Event()
    allow_first = Event()
    second_entered_load = Event()

    def interleave(handle, *args, **kwargs):
        if handle is storage._handle:
            if current_thread().name.startswith("first-auth-read"):
                first_in_load.set()
                assert allow_first.wait(5)
            elif current_thread().name.startswith("second-auth-read"):
                second_entered_load.set()
        return real_load(handle, *args, **kwargs)

    monkeypatch.setattr(storages.json, "load", interleave)
    with (ThreadPoolExecutor(1, thread_name_prefix="first-auth-read") as first,
          ThreadPoolExecutor(1, thread_name_prefix="second-auth-read") as second):
        # A second raw read consumes the same file handle between seek and load.
        raw = first.submit(storage.read)
        assert first_in_load.wait(5)
        try:
            assert second.submit(storage.read).result(5) is not None
            assert second_entered_load.is_set()
        finally:
            allow_first.set()
        with pytest.raises(JSONDecodeError):
            raw.result(5)

        first_in_load.clear()
        allow_first.clear()
        second_entered_load.clear()
        second_request_started = Event()

        def existing_login_lookup():
            second_request_started.set()
            return auth_services.get_user_in_db_by_email("concurrency@example.test")

        sse = first.submit(auth_security._find_user_by_id, "qa-concurrency")
        assert first_in_load.wait(5)
        login = second.submit(existing_login_lookup)
        try:
            assert second_request_started.wait(5)
            assert not second_entered_load.wait(0.05)
        finally:
            allow_first.set()
        assert sse.result(5).id == "qa-concurrency"
        assert login.result(5).id == "qa-concurrency"
        assert second_entered_load.is_set()


def test_auth_read_modify_write_serializes_same_email_registration(isolated_auth_db, monkeypatch):
    first_hash_started = Event()
    finish_first_hash = Event()
    second_registration_started = Event()
    second_hash_started = Event()

    def hash_password(value):
        if value == "first-test-password":
            first_hash_started.set()
            assert finish_first_hash.wait(5)
        else:
            second_hash_started.set()
        return "test-hash"

    def register_second():
        second_registration_started.set()
        return auth_services.create_user(UserCreate(
            email="one@example.test", password="second-test-password",
        ))

    monkeypatch.setattr(auth_services, "hash_password", hash_password)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(auth_services.create_user, UserCreate(
            email="one@example.test", password="first-test-password",
        ))
        assert first_hash_started.wait(5)
        second = executor.submit(register_second)
        try:
            assert second_registration_started.wait(5)
            assert not second_hash_started.wait(0.05)
        finally:
            finish_first_hash.set()
        assert first.result(5).email == "one@example.test"
        with pytest.raises(ValueError, match="already exists"):
            second.result(5)
    assert len(isolated_auth_db.users.all()) == 1


def test_auth_reset_token_is_single_use_under_concurrency(isolated_auth_db, monkeypatch):
    isolated_auth_db.users.insert({
        "id": "qa-reset-concurrency", "email": "reset-concurrency@example.test",
        "hashed_password": "old-test-hash", "role": "user", "is_active": True,
    })
    token = auth_services.issue_password_reset_token("qa-reset-concurrency")
    first_inside_reset = Event()
    release_reset = Event()
    second_inside_reset = Event()

    def hash_password(value):
        if value == "first-test-password":
            first_inside_reset.set()
            assert release_reset.wait(5)
        else:
            second_inside_reset.set()
        return value

    monkeypatch.setattr(auth_services, "hash_password", hash_password)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(auth_services.reset_password, token, "first-test-password")
        assert first_inside_reset.wait(5)
        second = executor.submit(auth_services.reset_password, token, "second-test-password")
        try:
            assert not second_inside_reset.wait(0.05)
        finally:
            release_reset.set()
        first.result(5)
        with pytest.raises(ValueError, match="already been used"):
            second.result(5)
    assert isolated_auth_db.users.all()[0]["hashed_password"] == "first-test-password"
