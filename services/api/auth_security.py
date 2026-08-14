"""
TrackFlow Auth security — password hashing utilities.

Uses bcrypt via passlib for all password operations.
"""

from __future__ import annotations

from passlib.hash import bcrypt


def hash_password(password: str) -> str:
    """Hash a plain-text password using bcrypt.

    Args:
        password: The plain-text password to hash.

    Returns:
        The bcrypt-hashed password string.
    """
    return bcrypt.hash(password)


def verify_password(password: str, hashed_password: str) -> bool:
    """Verify a plain-text password against a bcrypt hash.

    Args:
        password: The plain-text password to check.
        hashed_password: The bcrypt hash to verify against.

    Returns:
        True if the password matches the hash, False otherwise.
    """
    return bcrypt.verify(password, hashed_password)