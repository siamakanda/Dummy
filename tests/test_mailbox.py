import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from mailbox import ingest, parse_email
from store import Store

EMAILS = pathlib.Path(__file__).parent / "fixtures" / "emails"


def load(name):
    return parse_email((EMAILS / name).read_bytes())


def test_ani_unwraps_to_carrier_and_has_attachment():
    parsed = load("ani.eml")
    assert parsed.sender == "rates@ani-network.net"          # not the NOC forwarder
    assert parsed.subject.startswith("Fwd: Notice of Modification")
    assert parsed.message_id == "ani-100740-1001@instacall.digital"
    assert "ANI_10_28_2026_rates.csv" in [a.filename for a in parsed.attachments]


def test_nuso_zip_attachment_and_older_quote():
    parsed = load("nuso.eml")
    assert parsed.sender == "billing@nuso.com"
    assert any(a.filename.endswith(".csv.zip") for a in parsed.attachments)
    assert "BLC_Platinum_Q3_2026_10_15.csv.zip" in parsed.body_text


def test_dialworld_link_and_tech_prefix():
    parsed = load("dialworld.eml")
    assert parsed.sender == "rates@dialworld.com"
    assert "Tech Prefix: 6060" in parsed.body_text
    assert any("6060-11-01.csv" in url for url in parsed.download_links())


def test_fractel_has_csv_attachment():
    parsed = load("fractel.eml")
    assert parsed.sender == "rates@fractel.com"
    assert any(a.filename.endswith(".csv") for a in parsed.attachments)


def test_nested_forward_resolves_innermost_sender():
    parsed = load("nested_forward.eml")
    assert parsed.sender == "rates@ani-network.net"
    assert any(a.filename.endswith(".csv") for a in parsed.attachments)


def test_ingest_is_idempotent_by_message_id(tmp_path):
    store = Store(tmp_path / "dummy.db")
    raw = (EMAILS / "ani.eml").read_bytes()
    first = ingest(store, raw)
    assert first is not None and first.sender == "rates@ani-network.net"
    assert ingest(store, raw) is None


class FakeIMAP:
    def __init__(self, host, port):
        self.host, self.port = host, port
        self.user = None
        self.folder = None
        self.fetched = []

    def login(self, user, password):
        self.user = user
        return "OK", [b""]

    def select(self, folder):
        self.folder = folder
        return "OK", [b"4"]

    def search(self, charset, criteria):
        if criteria == "ALL":
            return "OK", [b"1 2 3 4"]
        return "OK", [b"2 3"]

    def fetch(self, number, spec):
        self.fetched.append(spec)
        return "OK", [(b"1 (RFC822 {5}", b"hello")]

    def list(self):
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren) "/" "Archive"']

    def logout(self):
        return "BYE", [b""]


def test_mailbox_check_reports_counts(monkeypatch, capsys):
    import mailbox as mailbox_mod
    monkeypatch.setattr(mailbox_mod.imaplib, "IMAP4_SSL", FakeIMAP)

    rc = mailbox_mod.main(["--check", "--host", "imap.test", "--user", "u", "--password", "p"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "imap.test:993" in out
    assert "4 message(s)" in out and "2 unseen" in out


def test_mailbox_check_lists_folders(monkeypatch, capsys):
    import mailbox as mailbox_mod
    monkeypatch.setattr(mailbox_mod.imaplib, "IMAP4_SSL", FakeIMAP)

    rc = mailbox_mod.main(["--check", "--folders", "--host", "h", "--user", "u", "--password", "p"])

    assert rc == 0
    assert "Archive" in capsys.readouterr().out


def test_fetch_raw_marks_seen_by_default(monkeypatch):
    import mailbox as mailbox_mod
    monkeypatch.setattr(mailbox_mod.imaplib, "IMAP4_SSL", FakeIMAP)

    box = mailbox_mod.Mailbox(host="h", user="u", password="p")
    with box:
        raws = box.fetch_raw()

    assert raws == [b"hello", b"hello"]              # FakeIMAP reports 2 unseen
    assert set(box.conn.fetched) == {"(RFC822)"}


def test_fetch_raw_peek_when_mark_seen_false(monkeypatch):
    import mailbox as mailbox_mod
    monkeypatch.setattr(mailbox_mod.imaplib, "IMAP4_SSL", FakeIMAP)

    box = mailbox_mod.Mailbox(host="h", user="u", password="p", mark_seen=False)
    with box:
        box.fetch_raw()

    assert set(box.conn.fetched) == {"(BODY.PEEK[])"}


def test_mailbox_check_reports_failure(monkeypatch, capsys):
    import mailbox as mailbox_mod

    class BadIMAP(FakeIMAP):
        def login(self, user, password):
            raise mailbox_mod.imaplib.IMAP4.error("authentication failed")

    monkeypatch.setattr(mailbox_mod.imaplib, "IMAP4_SSL", BadIMAP)

    rc = mailbox_mod.main(["--check", "--host", "h", "--user", "u", "--password", "bad"])

    assert rc == 1
    assert "failed" in capsys.readouterr().err

