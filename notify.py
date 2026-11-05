"""Outbound notifications (M14): console/log always, plus email (SMTP) when configured.

Email is configured via ``SMTP_*`` in ``.env``. A failure is logged and never breaks the pipeline.
``get_notifier()`` returns a cached default instance.
"""
from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

import config

log = logging.getLogger("dummy.notify")


class Notifier:
    def __init__(self, *, smtp_host=None, smtp_port=None, smtp_user=None, smtp_password=None,
                 smtp_from=None, smtp_to=None, out=print):
        self.smtp_host = smtp_host if smtp_host is not None else config.SMTP_HOST
        self.smtp_port = smtp_port if smtp_port is not None else config.SMTP_PORT
        self.smtp_user = smtp_user if smtp_user is not None else config.SMTP_USER
        self.smtp_password = smtp_password if smtp_password is not None else config.SMTP_PASSWORD
        self.smtp_from = smtp_from if smtp_from is not None else config.SMTP_FROM
        self.smtp_to = smtp_to if smtp_to is not None else config.SMTP_TO
        self.out = out

    def send(self, text: str) -> None:
        log.info(text)
        try:
            self.out(text)
        except Exception:
            log.exception("console notification failed")
        self._email(text)

    def _email(self, text: str) -> None:
        if not (self.smtp_host and self.smtp_to):
            return
        try:
            message = EmailMessage()
            message["Subject"] = "Dummy notification"
            message["From"] = self.smtp_from or self.smtp_user
            message["To"] = self.smtp_to
            message.set_content(text)
            port = int(self.smtp_port)
            server_cls = smtplib.SMTP_SSL if port == 465 else smtplib.SMTP   # 465 = implicit SSL
            with server_cls(self.smtp_host, port, timeout=10) as server:
                if port != 465:
                    server.starttls()
                if self.smtp_user:
                    server.login(self.smtp_user, self.smtp_password)
                server.send_message(message)
        except Exception:
            log.exception("email notification failed")


_instance: Notifier | None = None


def get_notifier() -> Notifier:
    global _instance
    if _instance is None:
        _instance = Notifier()
    return _instance
