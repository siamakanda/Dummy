import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import notify
from notify import Notifier


def test_email_channel(monkeypatch):
    sent = []

    class FakeSMTP:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def starttls(self):
            pass

        def login(self, *args):
            pass

        def send_message(self, message):
            sent.append(message)

    monkeypatch.setattr(notify.smtplib, "SMTP", FakeSMTP)

    Notifier(smtp_host="smtp.example", smtp_port=587, smtp_user="u", smtp_password="p",
             smtp_from="dummy@example.com", smtp_to="ops@example.com",
             out=lambda *_: None).send("body")

    assert sent and sent[0]["To"] == "ops@example.com"
    assert sent[0]["From"] == "dummy@example.com"


def test_no_email_configured_is_safe():
    Notifier(smtp_host="", smtp_to="", out=lambda *_: None).send("nothing configured")


def test_smtp_implicit_ssl_for_port_465(monkeypatch):
    used = {}

    class FakeSMTPSSL:
        def __init__(self, host, port, **kwargs):
            used["host"], used["port"] = host, port

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def login(self, *args):
            used["login"] = True

        def send_message(self, message):
            used["sent"] = message

    def boom(*args, **kwargs):
        raise AssertionError("plain SMTP must not be used on port 465")

    monkeypatch.setattr(notify.smtplib, "SMTP_SSL", FakeSMTPSSL)
    monkeypatch.setattr(notify.smtplib, "SMTP", boom)

    Notifier(smtp_host="mail.example", smtp_port=465, smtp_user="u", smtp_password="p",
             smtp_to="ops@example.com", out=lambda *_: None).send("body")

    assert used["port"] == 465 and "sent" in used
