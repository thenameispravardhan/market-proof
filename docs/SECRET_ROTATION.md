# Secret rotation runbook

Rotate every secret below when it may have been exposed: pasted into a chat
or an issue, committed by mistake, shown in a screen recording, or held by
someone who no longer works on the project. Rotate all of them together; a
half-rotated set is the one that gets forgotten.

Order matters. Create the new credential, switch the bot to it, check it
works, and only then revoke the old one. That way the bot is never down
between the two.

| Secret | Where it lives | Rotate | Revoke the old one |
|---|---|---|---|
| SSH key for the server | your machine (`deploy/target.local.env` names the path) | `ssh-keygen -t ed25519 -f ~/.ssh/tradebot_new`, then append the `.pub` to `~/.ssh/authorized_keys` on the server | Remove the old line from `~/.ssh/authorized_keys`. If the key was created by Lightsail, delete it under Lightsail → Account → SSH keys |
| DeepSeek API key | `.env` `DEEPSEEK_API_KEY` | platform.deepseek.com → API keys → create | Delete the old key on the same page |
| Fyers app secret | `.env` `FYERS_SECRET_KEY` | myapi.fyers.in → your app → regenerate secret. The app id stays the same | Regenerating replaces it. Then log in again (Accounts → Connect Fyers): the daily token is minted with the secret |
| Fyers postback secret | `.env` `FYERS_POSTBACK_SECRET` and the Fyers dashboard postback config | `python -c "import secrets; print(secrets.token_urlsafe(32))"` | Paste the new value in both places in one sitting |
| Telegram bot token | Notifications page (stored in the DB) | Message @BotFather, send `/revoke`, pick the bot, and copy the new token into the channel | `/revoke` invalidates the old token at once |
| Dashboard basic-auth password | `/etc/caddy/Caddyfile` | `caddy hash-password`, then replace the hash and run `sudo systemctl reload caddy` | The reload drops the old hash |
| AWS credentials for offsite backups | `~/.aws/credentials` on the server | IAM → user → Security credentials → create access key, then `aws configure` | Deactivate, then delete, the old key in IAM |

After editing `.env`, run `sudo systemctl restart tradebot` and check:

1. `curl -fsS http://127.0.0.1:8000/health` returns OK.
2. On the Dashboard, press **Re-check** on the readiness banner (or call
   `POST /api/system/fyers-selftest/run`). Every check should be `ok` or
   `skip`.
3. Send a test notification from the Notifications page.
4. Run `bash deploy/backup.sh` once by hand. `data/backups/status.json`
   should show `"offsite": "ok"`.

Then search the git history for the old values. If one was ever committed,
rotating it is the fix. Rewriting history does not make a published secret
safe again.
