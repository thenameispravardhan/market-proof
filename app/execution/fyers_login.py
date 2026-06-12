"""One-shot CLI to start the Fyers OAuth flow.

Run:

    python -m app.execution.fyers_login

This prints the Fyers authorize URL. Open it in a browser, log
in, approve. Fyers will redirect to FYERS_REDIRECT_URI
(default: http://localhost:8000/api/fyers/callback?auth_code=...&state=...).
The /api/fyers/callback endpoint handles the rest and stores the
token in the matching broker_accounts row.

This script does not print the URL on a successful auth — it only
prints the URL to open. The callback handler returns an HTML page
on success.
"""
from app.execution.fyers_auth import main

if __name__ == "__main__":
    raise SystemExit(main())
