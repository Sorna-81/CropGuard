"""Notification transport boundary; in-app delivery is always available."""
from datetime import datetime, timezone
from typing import Protocol


class NotificationProvider(Protocol):
    def send(self, db, user_id: int, title: str, message: str) -> bool: ...


class InAppNotificationProvider:
    """Persist an in-app notification; external providers can implement the same interface."""
    def send(self, db, user_id: int, title: str, message: str) -> bool:
        stamp = datetime.now(timezone.utc).isoformat()
        db.execute("INSERT INTO notifications(user_id,channel,status,title,message,created_at,sent_at) VALUES(?,?,?,?,?,?,?)",
                   (user_id, "in_app", "sent", title, message, stamp, stamp))
        return True


in_app_notifications = InAppNotificationProvider()
