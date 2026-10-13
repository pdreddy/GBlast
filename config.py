"""
FYERS credentials are read from a local `.env` file (or real environment variables) - never from the code.
Copy `.env.example` to `.env` (same folder as this file) and fill it in. `.env` and `access_token.txt` are git-ignored.
Get the App ID / Secret ID from https://myapi.fyers.in/dashboard.

Run `python3 config.py` to check what was found (it never prints the secret itself).
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
# accepted names (first match wins) - so a slightly different spelling still works
ID_NAMES = ["FYERS_CLIENT_ID", "FYERS_APP_ID", "CLIENT_ID", "APP_ID"]
SECRET_NAMES = ["FYERS_SECRET_KEY", "FYERS_SECRET_ID", "FYERS_SECRET", "SECRET_KEY", "SECRET_ID"]
REDIRECT_NAMES = ["FYERS_REDIRECT_URI", "REDIRECT_URI"]
CANDIDATE_FILES = [".env", ".env.txt", "env", "env.txt"]       # .env.txt = TextEdit/Notepad accidentally adding .txt

ENV_FILE_USED = None
KEYS_SEEN = []


def _load_env():
    global ENV_FILE_USED
    for name in CANDIDATE_FILES:
        path = os.path.join(HERE, name)
        if not os.path.isfile(path):
            continue
        ENV_FILE_USED = path
        with open(path, encoding="utf-8-sig") as f:               # utf-8-sig strips a hidden BOM
            for line in f:
                line = line.strip().lstrip("﻿")
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.lower().startswith("export "):
                    line = line[7:].strip()
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip()
                if v[:1] in "\"'" and v.find(v[0], 1) > 0:
                    v = v[1:v.find(v[0], 1)]                       # quoted value (anything after the closing quote is ignored)
                elif " #" in v:
                    v = v.split(" #", 1)[0].strip()                # trailing comment
                KEYS_SEEN.append(k)
                if v and not os.environ.get(k):                    # real env vars win, but empty ones do not
                    os.environ[k] = v
        return


def _get(names, default=""):
    for n in names:
        v = os.environ.get(n, "").strip()
        if v:
            return v
    return default


_load_env()

CLIENT_ID = _get(ID_NAMES)                                    # App ID, e.g. "AB1CD2EF3G-100"
SECRET_KEY = _get(SECRET_NAMES)                               # Secret ID of the same app
REDIRECT_URI = _get(REDIRECT_NAMES, "https://trade.fyers.in/api-login/redirect-uri/index.html")


def explain():
    """Human-readable diagnosis used by fyers_login.py when credentials are missing."""
    lines = [f"Looked in: {HERE}"]
    if ENV_FILE_USED:
        lines.append(f"Found file: {os.path.basename(ENV_FILE_USED)}   variable names in it: {KEYS_SEEN or 'none'}")
        if os.path.basename(ENV_FILE_USED) != ".env":
            lines.append("-> rename it to exactly '.env' (no .txt). In Terminal: mv %s .env" % os.path.basename(ENV_FILE_USED))
    else:
        lines.append("No .env file found there. Create it:  cp .env.example .env   then edit it.")
        lines.append("(Finder hides files starting with a dot: press Cmd+Shift+. to show them.)")
    lines.append(f"FYERS_CLIENT_ID: {'OK' if CLIENT_ID else 'MISSING'}   FYERS_SECRET_KEY: {'OK' if SECRET_KEY else 'MISSING'}")
    lines.append("Each line must look like:  FYERS_CLIENT_ID=AB1CD2EF3G-100   (no spaces around '=', no quotes needed)")
    return "\n".join(lines)


if __name__ == "__main__":
    print(explain())
    if CLIENT_ID:
        print(f"App ID read: {CLIENT_ID}")
    if SECRET_KEY:
        print(f"Secret read: {'*' * max(len(SECRET_KEY) - 2, 0)}{SECRET_KEY[-2:]}  ({len(SECRET_KEY)} characters)")
