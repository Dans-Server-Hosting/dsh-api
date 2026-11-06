"""User feedback: anyone signed in can leave it, admins read and triage it."""

from __future__ import annotations

from datetime import timedelta

from dsh_api.db import Database, FeedbackRow

STATUSES = ("new", "read")
RATE_LIMIT = 10
RATE_WINDOW = timedelta(hours=1)


class FeedbackNotFound(Exception):
    pass


class FeedbackRateLimited(Exception):
    def __init__(self, limit: int, window: timedelta) -> None:
        super().__init__(f"more than {limit} feedback messages in {window}")
        self.limit = limit
        self.window = window


class FeedbackService:
    def __init__(self, db: Database) -> None:
        self.db = db

    def submit(self, username: str, message: str, page: str | None) -> FeedbackRow:
        row = self.db.reserve_feedback(username, message, page, RATE_LIMIT, RATE_WINDOW)
        if row is None:
            raise FeedbackRateLimited(RATE_LIMIT, RATE_WINDOW)
        self.db.record(username, None, "feedback", str(row.id))
        return row

    def list(self, status: str) -> list[FeedbackRow]:
        """``status`` is one of the statuses or ``all``; newest first."""
        return self.db.list_feedback(None if status == "all" else status)

    def set_status(self, feedback_id: int, status: str) -> FeedbackRow:
        if self.db.get_feedback(feedback_id) is None:
            raise FeedbackNotFound(feedback_id)
        return self.db.set_feedback_status(feedback_id, status)  # type: ignore[return-value]
