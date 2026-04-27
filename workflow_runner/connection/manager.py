"""In-process session pool — create, retrieve, and remove SSH sessions."""

from __future__ import annotations

from typing import Optional

from workflow_runner.connection.session import Session, SessionConfig
from workflow_runner.logger import get_logger


class SessionManager:
    """
    Lifecycle manager for named SSH sessions.

    Sessions are identified by a user-supplied *session_id* string.
    The manager does not own the SSH transport; each :class:`Session`
    manages its own connection.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._log = get_logger("connection.manager")

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    @property
    def sessions(self) -> dict[str, Session]:
        """Read-only snapshot of current sessions."""
        return dict(self._sessions)

    def create(self, session_id: str, config: SessionConfig) -> Session:
        """
        Register a new session.

        Raises :class:`ValueError` if *session_id* is already registered.
        Does **not** connect — call :meth:`Session.connect` separately.
        """
        if session_id in self._sessions:
            raise ValueError(f"Session '{session_id}' already exists. Use a unique ID.")
        session = Session(session_id=session_id, config=config)
        self._sessions[session_id] = session
        self._log.info("Registered session '%s' → %s", session_id, session.label)
        return session

    def get(self, session_id: str) -> Optional[Session]:
        """Return the session or None if not registered."""
        return self._sessions.get(session_id)

    def remove(self, session_id: str) -> None:
        """Disconnect and deregister a session. No-op if unknown."""
        session = self._sessions.pop(session_id, None)
        if session:
            session.disconnect()
            self._log.info("Removed session '%s'", session_id)

    def disconnect_all(self) -> None:
        """Disconnect every session (e.g. on programme exit)."""
        for session in list(self._sessions.values()):
            session.disconnect()
        self._sessions.clear()

    def list_ids(self) -> list[str]:
        return list(self._sessions.keys())
