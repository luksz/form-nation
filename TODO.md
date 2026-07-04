# To-do

## Needs Lucas (in priority order)

- [ ] **Collect blank claim forms from 3–4 other insurers** and register them
      as claim types — the single highest-value input; new layouts drive the
      next engineering round
- [ ] **Paid Gemini tier** (aistudio.google.com → key → link billing) —
      required before any real client data
- [ ] **FileVault on** on the Mac running FormNation
- [ ] **Firm compliance question** before real client data — hand over
      [SECURITY.md](SECURITY.md) + [docs/data-handling.md](docs/data-handling.md)
- [ ] Re-register the Personal Accident template (`/newtype Personal Accident`
      + blank GEG PDF) — current one works but is named `filled.pdf`
- [ ] Clear old test chats in Telegram (delivered PDFs auto-delete; your own
      sent messages don't)

## Engineering backlog (parked by priority)

- [ ] **Web feature parity**: start/register claim types, intake-notes
      extraction box, and read-back verify button in the web UI
      (backend refactor started: `ingest_pdf()` is ready for reuse)
- [ ] **VPS deployment** — Docker/Caddy packaging done and build-verified;
      needs a server + domain, then ~1 hour
- [ ] Tune Tier 2/3 CV detection against each new insurer's form as samples
      arrive (expect 60–80% on first contact, near-100% after tuning)
- [ ] Readable field names for Tier 2/3 side panel (currently raw text
      snippets)
- [ ] Report values that didn't fit their box (font auto-shrink exists,
      truncation reporting doesn't)
- [ ] Self-hosted Telegram Bot API server — only if firm compliance requires
      keeping chat traffic off Telegram's cloud
- [ ] Multi-language forms (Chinese/Malay/Tamil sections) — OCR + prompts
      untested
- [ ] If productized for other advisers: multi-user auth, per-user data
      isolation, rate limiting, PDPA data-intermediary review

## Done (highlights)

- M0–M4: tiered pipeline (AcroForm / flattened / scan+OCR), set-of-marks
  design validated by benchmark, checkboxes 21/21
- Telegram bot: conversational intake with gap tracking, claim types,
  auto-saved clients, read-back check in every fill summary — proven live
- Web UI + dashboard, click-to-place fallback, validators (NRIC checksum)
- Security: PII redaction before LLM, encrypted client DB, retention +
  chat auto-delete, deletion controls, auth, injection guards, Docker
