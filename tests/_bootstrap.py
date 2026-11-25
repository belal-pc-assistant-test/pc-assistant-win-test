"""Import helper for the remote-session tests: project path and an isolated token store."""

import os
import sys
import tempfile
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))
os.environ.setdefault("TPA_TOKEN_STORE_DIR", tempfile.mkdtemp(prefix="tpa-remote-store-"))
os.environ.pop("TELEGRAM_BOT_TOKEN", None)
