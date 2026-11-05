"""Regenerate the .eml test fixtures from the sample CSVs.

    python tests/fixtures/emails/build_emails.py

These mirror the four sample emails in AGENTS.md section 7 (nested forwards, zip attachment,
download link, '#' header) plus a 3-layer forward attached as message/rfc822.
"""
import io
import pathlib
import zipfile
from email.message import EmailMessage
from email.mime.message import MIMEMessage

HERE = pathlib.Path(__file__).parent
FIXTURES = HERE.parent


def _message(sender, to, subject, body, message_id, date="Tue, 28 Oct 2026 09:00:00 +0000"):
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = date
    msg["Message-ID"] = message_id
    msg.set_content(body)
    return msg


def _attach(msg, filename, data, content_type):
    maintype, subtype = content_type.split("/", 1)
    msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)


def build_ani():
    csv = (FIXTURES / "ANI_10_28_2026_rates.csv").read_bytes()
    body = (
        "FYI - can you update this deck?\n\n"
        "---------- Forwarded message ---------\n"
        "From: ANI Billing <rates@ani-network.net>\n"
        "Date: Tue, 28 Oct 2026 07:12:00 +0000\n"
        "Subject: Notice of Modification 100740 1001\n\n"
        "Attached is the updated rate notice. Pricing is effective Oct 29, 2026.\n"
    )
    msg = _message("NOC <noc@instacall.digital>", "dummy@instacall.digital",
                   "Fwd: Notice of Modification 100740 1001", body,
                   "<ani-100740-1001@instacall.digital>")
    _attach(msg, "ANI_10_28_2026_rates.csv", csv, "text/csv")
    (HERE / "ani.eml").write_bytes(msg.as_bytes())


def build_nuso():
    csv = (FIXTURES / "BLC_Platinum_Plus_Q3_2026_11_02.csv").read_bytes()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("BLC_Platinum_Plus_Q3_2026_11_02.csv", csv)
    body = (
        "---------- Forwarded message ---------\n"
        "From: Nuso Billing <billing@nuso.com>\n"
        "Subject: TRM_INSTACALL_PLAT - Platinum Plus rate update\n\n"
        "Please find the Platinum Plus update attached. Unlisted NPANXX bill at $0.06.\n"
        "Indeterminate calls are rated as Interstate. Billing change 6/6 -> 12/6 effective 2026-11-01.\n\n"
        "---------- Older notice (superseded) ---------\n"
        "From: Nuso Billing <billing@nuso.com>\n"
        "Subject: TRM_INSTACALL_PLAT - previous\n"
        "Attachment: BLC_Platinum_Q3_2026_10_15.csv.zip\n"
    )
    msg = _message("Siam <siam@instacall.digital>", "dummy@instacall.digital",
                   "Fwd: TRM_INSTACALL_PLAT", body, "<nuso-trm-1102@instacall.digital>")
    _attach(msg, "BLC_Platinum_Plus_Q3_2026_11_02.csv.zip", buffer.getvalue(), "application/zip")
    (HERE / "nuso.eml").write_bytes(msg.as_bytes())


def build_dialworld():
    body = (
        "---------- Forwarded message ---------\n"
        "From: Dial World Rates <rates@dialworld.com>\n"
        "Subject: DIAL WORLD Prefix 6060 rate deck 11/01/2026\n\n"
        "Trunk: USA CONVO (C)\n"
        "Tech Prefix: 6060\n"
        "Effective: 2026-11-01\n\n"
        "Download Now: https://files.dialworld.example/decks/6060-11-01.csv?token=abc123\n"
        "This link is valid until 2026-11-01.\n"
    )
    msg = _message("NOC <noc@instacall.digital>", "dummy@instacall.digital",
                   "Fwd: DIAL WORLD Prefix 6060", body, "<dw-6060-1101@instacall.digital>")
    (HERE / "dialworld.eml").write_bytes(msg.as_bytes())


def build_fractel():
    csv = (FIXTURES / "Fractel-2026-11-01-CVDeluxe-Domestic.csv").read_bytes()
    body = (
        "---------- Forwarded message ---------\n"
        "From: Fractel Rates <rates@fractel.com>\n"
        "Subject: Fractel CVDeluxe Domestic - effective 2026-11-01\n\n"
        "Attached. Effective Date: 2026-11-01. Note the 6-second short-duration penalty terms.\n"
    )
    msg = _message("Sohail <sohail@instacall.digital>", "dummy@instacall.digital",
                   "Fwd: Fractel CVDeluxe Domestic", body, "<fractel-1101@instacall.digital>")
    _attach(msg, "Fractel-2026-11-01-CVDeluxe-Domestic.csv", csv, "text/csv")
    (HERE / "fractel.eml").write_bytes(msg.as_bytes())


def build_nested():
    csv = (FIXTURES / "ANI_10_28_2026_rates.csv").read_bytes()
    carrier = _message("ANI Billing <rates@ani-network.net>", "noc@instacall.digital",
                       "Notice of Modification 100740 1001", "Attached rate notice.\n",
                       "<ani-carrier@ani-network.net>")
    _attach(carrier, "ANI_10_28_2026_rates.csv", csv, "text/csv")

    noc = _message("NOC <noc@instacall.digital>", "founder@instacall.digital",
                   "Fwd: Notice of Modification 100740 1001", "Forwarding to you.\n",
                   "<noc-fwd@instacall.digital>")
    noc.make_mixed()
    noc.attach(MIMEMessage(carrier))

    founder = _message("Founder <founder@instacall.digital>", "dummy@instacall.digital",
                       "Fwd: Notice of Modification 100740 1001", "Please process.\n",
                       "<founder-fwd@instacall.digital>")
    founder.make_mixed()
    founder.attach(MIMEMessage(noc))
    (HERE / "nested_forward.eml").write_bytes(founder.as_bytes())


def main():
    for builder in (build_ani, build_nuso, build_dialworld, build_fractel, build_nested):
        builder()
        print("built", builder.__name__)


if __name__ == "__main__":
    main()
