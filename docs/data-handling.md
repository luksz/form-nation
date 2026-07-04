# Data handling — how personal data flows through form-nation

## Principles

1. **Data minimisation:** no raw chat transcripts are ever stored. The
   extracted structured profile *is* the record; the conversation is
   discarded once processed.
2. **The LLM sees as little as possible:** mapping decisions only need
   profile *keys*, so values are placeholder-redacted; pattern-matchable
   identifiers (NRIC/FIN, email, phone, account numbers) are redacted from
   intake messages too and restored locally (`app/redact.py`,
   `REDACT_BEFORE_LLM=true` by default).
3. **Short-lived by default:** anything containing filled personal data
   expires automatically.
4. **Deletable on request:** clients can be permanently removed via
   `/delclient` (bot) or the 🗑 button (web) — a PDPA data-subject-deletion
   requirement.

## Where data lives

| Store | Contents | Retention | Protection |
|---|---|---|---|
| `uploads/` | uploaded forms, field caches, filled PDFs | `RETENTION_HOURS` (24 h) | local disk only |
| `data/form-nation.db` clients | saved client profiles | until deleted | **Fernet-encrypted** (key: `data/.secret.key`, mode 600) |
| `data/form-nation.db` audit | mapping decisions: field ↔ profile-key + confidence | indefinite | contains **no values** by design |
| `data/templates/` | registered blank forms | indefinite | no personal data |
| Bot session (RAM) | in-progress claim + filled PDF bytes | until approve/discard/restart | memory only |
| Telegram chat | messages, previews, delivered PDF | delivered PDF auto-deleted after `DELETE_DELIVERED_MINUTES` (60); other messages remain until user deletes | Telegram cloud — **not E2E encrypted** |

## What leaves the machine

- **To Telegram:** everything sent/received in the bot chat. Mitigated by
  the delivered-PDF auto-delete; the FA should treat the chat itself as a
  data store and clear it periodically.
- **To Google Gemini:** intake message text and profile values **after
  redaction** (NRIC → `<NRIC_1>` etc.), plus form field labels. Names and
  addresses still pass through (they can't be pattern-redacted while being
  extracted) — therefore **real client data requires the paid Gemini tier**
  (inputs not used for training). Free tier is for synthetic test data only.
- **Nothing else.** No analytics, no third-party services, no server of ours.

## Transcript policy

Do not add conversation logging. If an audit question is ever "why was this
field filled with X", the answer is in the `audit` table (decision metadata)
plus the delivered PDF the FA reviewed and approved — not in a chat log.

## Known residual risks

1. Names/addresses reach Google unredacted (inherent to extraction) — paid
   tier + Google's no-training terms is the control.
2. Telegram chat history is the longest-lived copy of claim conversations.
3. `data/.secret.key` sits beside the DB it protects — meaningful against
   accidental DB-file leaks (backups, copies), not against an attacker with
   full disk access. FileVault covers the latter.
4. Single-user assumption: no auth/user isolation until deployment.
