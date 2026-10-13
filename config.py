"""
FYERS credentials are read from a local `.env` file (or real environment variables) - never from the code.
Copy `.env.example` to `.env` and fill it in. `.env` and `access_token.txt` are git-ignored.
Get the App ID / Secret ID from https://myapi.fyers.in/dashboard.
"""
import os


def _load_env(path=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")):
    """Tiny .env reader (no extra dependency). Real environment variables take priority."""
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()

CLIENT_ID = os.environ.get("FYERS_CLIENT_ID", "")      # App ID, e.g. "AB1CD2EF3G-100"
SECRET_KEY = os.environ.get("FYERS_SECRET_KEY", "")    # Secret ID of the same app
REDIRECT_URI = os.environ.get("FYERS_REDIRECT_URI",
                              "https://trade.fyers.in/api-login/redirect-uri/index.html")  # must match the app

# Strategy settings are in the CONFIG block of gamma_blast.py / expiry_blast.py
