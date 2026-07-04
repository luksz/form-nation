# Security & data-protection status

One page: what protects data today, and what MUST happen before real client
data touches this system. Detail in [docs/data-handling.md](docs/data-handling.md)
and [docs/production-notes.md](docs/production-notes.md).

## Already in place ✅

| Control | Where |
|---|---|
| PII redaction before every LLM call (NRIC/email/phone/account → placeholders, restored locally) | `app/redact.py`, on by default |
| Client records encrypted at rest (Fernet; key `data/.secret.key`, 0600) | `app/db.py` |
| Uploads + filled PDFs auto-delete after `RETENTION_HOURS` (24 h) | `app/main.py` |
| Delivered PDFs auto-delete from the Telegram chat (`DELETE_DELIVERED_MINUTES`, 60) | `app/telegram_bot.py` |
| Client deletion on request — `/delclient` (bot), 🗑 (web) | PDPA data-subject deletion |
| Audit log stores decisions (key ↔ field + confidence), never values | `app/db.py` |
| Access-key auth on web UI + API (`APP_ACCESS_TOKEN`); constant-time compares | `app/main.py` |
| Upload size cap (`MAX_UPLOAD_MB`) | `app/main.py` |
| Prompt-injection guard: document/message text declared as data-not-instructions; JSON-schema outputs; profile-key allowlist on all LLM responses | `app/llm.py` |
| Secrets in `.env` (gitignored); bot token no longer logged; no field values in logs | — |
| Signature fields never auto-filled; human review mandatory before export | `app/main.py`, both UIs |
| Server binds 127.0.0.1 in local mode | — |

## Before ANY real client data 🔴 (owner actions)

1. **Paid Gemini tier** — free-tier inputs may be used for Google training.
   aistudio.google.com → API key → link billing. Non-negotiable.
2. **Your FA firm's compliance sign-off.** Under MAS/FAA rules and firm
   policy, client PII in unapproved third-party tools (Telegram, Google) is
   commonly restricted. Check before use — this is likely the binding
   constraint, above anything technical.
3. **FileVault on** on the Mac running it (System Settings → Privacy &
   Security) — covers everything at rest that app-level encryption doesn't.
4. Accept (or reject) that **Telegram servers carry the chat traffic** —
   Bot API is not end-to-end encrypted. Self-hosting the Bot API server is
   the mitigation if not.

## Before deploying off the laptop 🟡

- Set a strong `APP_ACCESS_TOKEN` (`python3 -c "import secrets; print(secrets.token_urlsafe(24))"`).
- HTTPS only (docker-compose ships Caddy for automatic certificates).
- Keep dependencies updated — PyMuPDF parses untrusted PDFs; its updates
  matter most (`pip list --outdated`).
- Server hygiene: firewall (only 80/443/SSH), unattended upgrades, no
  password SSH.

## Deliberately NOT done (and why that's OK for now)

- Rate limiting / multi-user isolation — single-user prototype on localhost.
- SOC2-style logging/monitoring — disproportionate at this stage.
- Self-hosted LLM — the redaction layer + paid tier is the pragmatic
  middle ground; revisit if a compliance review demands zero third-party
  processing.
