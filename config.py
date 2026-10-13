"""
Your FYERS API credentials - the ONLY file you need to edit before first run.
Get these from https://myapi.fyers.in/dashboard  (create an app -> App ID & Secret ID).
Keep this file private. Never share or upload it.
"""

CLIENT_ID = "zzz-100"        # App ID, e.g. "AB1CD2EF3G-100"
SECRET_KEY = "zz"      # Secret ID of the same app
REDIRECT_URI = "https://trade.fyers.in/api-login/redirect-uri/index.html"  # must match the app's redirect URL

# Strategy settings (capital, risk, stop/target, indices) are in the CONFIG block of gamma_blast.py
