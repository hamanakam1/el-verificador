"""Request and response schemas for the users API."""
from dataclasses import dataclass, field


@dataclass
class UserCreate:
    """A validated registration request."""

    username: str
    password: str
    email: str = ""


@dataclass
class UserRecord:
    """A persisted user."""

    username: str
    password_hash: str
    email: str = ""
    active: bool = True
    tags: list[str] = field(default_factory=list)
