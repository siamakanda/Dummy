import os
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")

BASE_URL = os.getenv("PORTAL_BASE_URL", "https://switchportal.instacall.digital").rstrip("/")
USERNAME = os.getenv("PORTAL_USERNAME", "")
PASSWORD = os.getenv("PORTAL_PASSWORD", "")
TRUNK_NAME_COLUMN = os.getenv("TRUNK_NAME_COLUMN", "").strip().lower()

# Rate-deck conversion defaults (AGENTS.md section 3)
# ISO is what the portal's template CSV and upload form use.
EFFECTIVE_DATE_FORMAT = os.getenv("EFFECTIVE_DATE_FORMAT", "%Y-%m-%d")
DEFAULT_INIT_DURATION = 12
DEFAULT_INCREMENT = 6

DATA_DIR = ROOT / "data"
SNAP_DIR = DATA_DIR / "snapshots"
STATE_DIR = ROOT / ".state"          # saved browser session (cookies) so we log in once
for d in (DATA_DIR, SNAP_DIR, STATE_DIR):
    d.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "dummy.db"
MATCH_THRESHOLD = float(os.getenv("MATCH_THRESHOLD", "0.85"))

# Portal writer (M4): the only trunk we may write to during development.
DEV_TRUNK_NAME = "BTCL TNT TRUNK"
DECK_NAME_DATE_FORMAT = os.getenv("DECK_NAME_DATE_FORMAT", "%m_%d_%Y")

# Mailbox + archive (M5)
ARCHIVE_DIR = DATA_DIR / "archive"
IMAP_HOST = os.getenv("IMAP_HOST", "")
IMAP_PORT = int(os.getenv("IMAP_PORT", "993"))
IMAP_USER = os.getenv("IMAP_USER", "")
IMAP_PASSWORD = os.getenv("IMAP_PASSWORD", "")
IMAP_FOLDER = os.getenv("IMAP_FOLDER", "INBOX")
IMAP_MARK_SEEN = os.getenv("IMAP_MARK_SEEN", "true").strip().lower() not in ("0", "false", "no")

# Email understanding (M6): any OpenAI-compatible endpoint; DeepSeek is the default.
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.deepseek.com")
LLM_API_KEY = (os.getenv("LLM_API_KEY") or os.getenv("DEEPSEEK_LLM")
               or os.getenv("DEEPSEEK_API_KEY") or "")
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-chat")

# Orchestrator (M7)
ORCH_INTERVAL = int(os.getenv("ORCH_INTERVAL", "300"))
AUDIT_LOG = DATA_DIR / "audit.jsonl"

# Web admin (M10)
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
SESSION_SECRET = os.getenv("SESSION_SECRET", "")
WEB_HOST = os.getenv("WEB_HOST", "127.0.0.1")
WEB_PORT = int(os.getenv("WEB_PORT", "8080"))

# Logging (M13)
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE = os.getenv("LOG_FILE", str(DATA_DIR / "dummy.log")) or None

# Notifications (M14) — email (SMTP); console/log is always on.
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("SMTP_FROM", "")
SMTP_TO = os.getenv("SMTP_TO", "")

def require_credentials():
    if not USERNAME or not PASSWORD:
        raise SystemExit("Set PORTAL_USERNAME and PORTAL_PASSWORD in .env")
