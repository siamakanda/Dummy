"""Read rate-deck emails: parse MIME, unwrap human forward layers, collect attachments/links.

AGENTS.md section 7: emails arrive wrapped in 2-3 "Forwarded message" layers, so the real carrier
is the **innermost** From:. Pure parsing functions are unit-testable; the IMAP client is a thin
wrapper used by the orchestrator (M7).
"""
from __future__ import annotations

import argparse
import email
import email.policy
import hashlib
import imaplib
import re
import sys
from dataclasses import dataclass, field
from email.utils import parseaddr

import config

FORWARD_MARKERS = (
    "---------- forwarded message ---------",
    "begin forwarded message",
    "-----original message-----",
    "----- original message -----",
)
FROM_RE = re.compile(r"^\s*from:\s*(.+)$", re.IGNORECASE | re.MULTILINE)
URL_RE = re.compile(r"https?://[^\s<>\"')]+", re.IGNORECASE)
HREF_RE = re.compile(r"""href=["'](https?://[^"']+)["']""", re.IGNORECASE)
DOWNLOAD_HINTS = (".csv", ".zip", ".xlsx", "download")

@dataclass
class Attachment:
    filename: str
    content_type: str
    payload: bytes

@dataclass
class ParsedEmail:
    message_id: str
    subject: str
    sender: str
    date: str
    body_text: str
    attachments: list[Attachment] = field(default_factory=list)
    links: list[str] = field(default_factory=list)

    def download_links(self) -> list[str]:
        return [url for url in self.links if any(h in url.lower() for h in DOWNLOAD_HINTS)]

def _clean_from(value: str) -> str:
    _, address = parseaddr(value or "")
    return address or (value or "").strip()

def _body_text(msg) -> str:
    chunks = []
    for part in msg.walk():
        if part.get_content_maintype() != "text" or part.get_filename():
            continue
        try:
            content = part.get_content()
        except Exception:
            payload = part.get_payload(decode=True) or b""
            content = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        if part.get_content_subtype() == "html":
            content = re.sub(r"<[^>]+>", " ", content)
        chunks.append(str(content))
    return "\n".join(chunks)

def unwrap_forward(msg) -> str:
    """Return the innermost (real carrier) sender, past the human forward layers."""
    for part in msg.walk():                                  # forwarded as an attached message
        if part.get_content_type() == "message/rfc822":
            payload = part.get_payload()
            inner = payload[0] if isinstance(payload, list) and payload else payload
            if isinstance(inner, email.message.Message):
                return unwrap_forward(inner)
    matches = FROM_RE.findall(_body_text(msg))               # text-forwarded headers in the body
    if matches:
        return _clean_from(matches[-1])                      # last = innermost
    return _clean_from(msg.get("From", ""))

def extract_links(text: str) -> list[str]:
    found = HREF_RE.findall(text) + URL_RE.findall(text)
    seen: set[str] = set()
    links = []
    for url in found:
        url = url.rstrip(".,;)>")
        if url and url not in seen:
            seen.add(url)
            links.append(url)
    return links

def _attachments(msg) -> list[Attachment]:
    out = []
    for part in msg.walk():
        filename = part.get_filename()
        if not filename:
            continue
        out.append(Attachment(filename=filename, content_type=part.get_content_type(),
                              payload=part.get_payload(decode=True) or b""))
    return out

def parse_email(raw: bytes) -> ParsedEmail:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    body = _body_text(msg)
    return ParsedEmail(
        message_id=str(msg.get("Message-ID", "")).strip().strip("<>"),
        subject=str(msg.get("Subject", "")).strip(),
        sender=unwrap_forward(msg),
        date=str(msg.get("Date", "")).strip(),
        body_text=body,
        attachments=_attachments(msg),
        links=extract_links(body),
    )

def ingest(store, raw: bytes) -> ParsedEmail | None:
    """Parse an email and record it; returns None when the Message-ID was already processed."""
    parsed = parse_email(raw)
    if not parsed.message_id:
        parsed.message_id = "sha1:" + hashlib.sha1(raw).hexdigest()
    if not store.add_message(parsed.message_id, sender=parsed.sender, subject=parsed.subject):
        return None
    return parsed

class Mailbox:
    """Thin IMAP client (implicit TLS). Configure via IMAP_* in .env."""

    def __init__(self, host=None, port=None, user=None, password=None, folder=None, mark_seen=None):
        self.host = host or config.IMAP_HOST
        self.port = port or config.IMAP_PORT
        self.user = user or config.IMAP_USER
        self.password = password or config.IMAP_PASSWORD
        self.folder = folder or config.IMAP_FOLDER
        self.mark_seen = config.IMAP_MARK_SEEN if mark_seen is None else mark_seen
        self.conn: imaplib.IMAP4_SSL | None = None

    def __enter__(self) -> "Mailbox":
        if not self.host or not self.user:
            raise SystemExit("Set IMAP_HOST / IMAP_USER / IMAP_PASSWORD in .env")
        self.conn = imaplib.IMAP4_SSL(self.host, self.port)
        self.conn.login(self.user, self.password)
        self.conn.select(self.folder)
        return self

    def __exit__(self, *exc) -> None:
        try:
            if self.conn:
                self.conn.logout()
        except Exception:
            pass

    def fetch_raw(self, criteria: str = "UNSEEN") -> list[bytes]:
        """Return the raw RFC822 bytes of matching messages (marks them seen unless mark_seen=False)."""
        _, data = self.conn.search(None, criteria)
        spec = "(RFC822)" if self.mark_seen else "(BODY.PEEK[])"
        raws = []
        for number in data[0].split():
            _, message_data = self.conn.fetch(number, spec)
            for part in message_data:
                if isinstance(part, tuple):
                    raws.append(part[1])
        return raws

    def _count(self, criteria: str) -> int:
        _, data = self.conn.search(None, criteria)
        return len(data[0].split()) if data and data[0] else 0

    def status(self, folder: str | None = None) -> dict:
        """Select a folder and report message / unseen counts (used by the --check CLI)."""
        folder = folder or self.folder
        self.conn.select(folder)
        return {"folder": folder, "messages": self._count("ALL"), "unseen": self._count("UNSEEN")}

    def folders(self) -> list[str]:
        _, data = self.conn.list()
        return [line.decode("utf-8", "replace") if isinstance(line, bytes) else str(line)
                for line in (data or [])]


def main(argv: list[str] | None = None) -> int:
    """`python mailbox.py --check` — connect with the .env settings and report, without processing."""
    parser = argparse.ArgumentParser(description="Check Dummy's IMAP mailbox (connect + report, no processing).")
    parser.add_argument("--check", action="store_true", help="connect and report (this is the default)")
    parser.add_argument("--folders", action="store_true", help="also list folders / labels")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--user")
    parser.add_argument("--password")
    parser.add_argument("--folder")
    args = parser.parse_args(argv)

    box = Mailbox(host=args.host, port=args.port, user=args.user, password=args.password,
                  folder=args.folder)
    try:
        with box:
            status = box.status()
            print(f"Connected to {box.host}:{box.port} as {box.user}")
            print(f"Folder {status['folder']}: {status['messages']} message(s), {status['unseen']} unseen")
            print(f"Mark-as-read on fetch: {'yes' if box.mark_seen else 'no (BODY.PEEK)'}")
            if args.folders:
                print("Folders:")
                for line in box.folders():
                    print(f"  {line}")
    except SystemExit:
        raise
    except Exception as exc:                       # bad host / credentials / TLS -> friendly message
        print(f"IMAP check failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
