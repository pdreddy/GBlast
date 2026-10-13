"""
Daily FYERS login (access tokens expire every day).
Creates access_token.txt, which gamma_blast.py reads.
Skips the login if today's token already exists.
"""
import datetime as dt
import os
import webbrowser

from fyers_apiv3 import fyersModel

from config import CLIENT_ID, SECRET_KEY, REDIRECT_URI

TOKEN_FILE = "access_token.txt"


def token_is_from_today():
    if not os.path.exists(TOKEN_FILE) or os.path.getsize(TOKEN_FILE) == 0:
        return False
    return dt.date.fromtimestamp(os.path.getmtime(TOKEN_FILE)) == dt.date.today()


def login():
    if "XXXX" in CLIENT_ID or "YOUR_" in SECRET_KEY:
        raise SystemExit("Edit config.py first: put in your FYERS App ID and Secret ID.")

    session = fyersModel.SessionModel(
        client_id=CLIENT_ID, secret_key=SECRET_KEY, redirect_uri=REDIRECT_URI,
        response_type="code", grant_type="authorization_code")

    url = session.generate_authcode()
    print("\nOpening the FYERS login page. Log in, then copy the 'auth_code' value")
    print("from the address bar of the page you land on.\n")
    print(url, "\n")
    try:
        webbrowser.open(url)
    except Exception:
        pass

    raw = input("Paste auth_code (or the whole redirected URL): ").strip()
    if "auth_code=" in raw:
        raw = raw.split("auth_code=")[1].split("&")[0]

    session.set_token(raw)
    resp = session.generate_token()
    if "access_token" not in resp:
        raise SystemExit(f"Login failed: {resp}")
    with open(TOKEN_FILE, "w") as f:
        f.write(resp["access_token"])
    print("Login OK - saved", TOKEN_FILE)


if __name__ == "__main__":
    if token_is_from_today():
        print("Today's access token already exists - skipping login.")
    else:
        login()
