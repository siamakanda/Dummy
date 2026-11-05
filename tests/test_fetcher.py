import pathlib
import sys
import zipfile

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import fetcher
from mailbox import Attachment, parse_email

EMAILS = pathlib.Path(__file__).parent / "fixtures" / "emails"


class FakeResponse:
    def __init__(self, content, headers=None):
        self.content = content
        self.headers = headers or {}
        self.text = content.decode("utf-8", "replace")

    def raise_for_status(self):
        return None


def load(name):
    return parse_email((EMAILS / name).read_bytes())


def test_save_attachment_sanitizes_name(tmp_path):
    attachment = Attachment("../../evil name.csv", "text/csv", b"a,b\n1,2\n")
    path = fetcher.save_attachment(attachment, tmp_path)
    assert path.name == "evil name.csv"
    assert path.read_bytes().startswith(b"a,b")


def test_extract_zip(tmp_path):
    archive = tmp_path / "deck.csv.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("inner/deck.csv", "a,b\n")
    out = fetcher.extract_zip(archive, tmp_path / "out")
    assert [p.name for p in out] == ["deck.csv"]
    assert out[0].read_text() == "a,b\n"


def test_download_url_writes_file(tmp_path, monkeypatch):
    response = FakeResponse(b"prefix,destination\n1201200,NANPA\n",
                            headers={"Content-Disposition": 'attachment; filename="rates.csv"'})
    monkeypatch.setattr(fetcher.requests, "get", lambda url, **kw: response)
    path = fetcher.download_url("https://x.example/rates.csv", tmp_path)
    assert path.name == "rates.csv"
    assert path.read_bytes().startswith(b"prefix")


def test_download_url_falls_back_to_browser(tmp_path, monkeypatch):
    def boom(url, **kwargs):
        raise RuntimeError("blocked")

    monkeypatch.setattr(fetcher.requests, "get", boom)
    seen = {}

    def fake_browser(url, dest_dir):
        seen["url"] = url
        out = pathlib.Path(dest_dir) / "from_browser.csv"
        out.write_bytes(b"x")
        return out

    monkeypatch.setattr(fetcher, "download_with_browser", fake_browser)
    path = fetcher.download_url("https://x.example/page", tmp_path, use_browser=True)
    assert seen["url"] == "https://x.example/page"
    assert path.name == "from_browser.csv"


def test_fetch_deck_files_unzips_nuso(tmp_path):
    files = fetcher.fetch_deck_files(load("nuso.eml"), tmp_path)
    assert len(files) == 1
    assert files[0].suffix == ".csv"
    assert files[0].read_text(encoding="utf-8-sig").startswith("1NPANXX")
