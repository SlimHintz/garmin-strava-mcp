"""Configure the server through env vars before it is imported, using a temp dir for all state."""

import os
import sys
import tempfile
from pathlib import Path

_tmp = Path(tempfile.mkdtemp(prefix="running-server-test-"))
os.environ.update({
    "API_KEY": "test-key-0123456789abcdef",  # gitleaks:allow (dummy key for tests)
    "PUBLIC_URL": "https://running.example.com",
    "CREDS_DIR": str(_tmp),
    "TRAINING_DB_PATH": str(_tmp / "training.db"),
    "OAUTH_DB_PATH": str(_tmp / "oauth.db"),
})
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
