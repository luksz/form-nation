"""Telegram bot for the FA workflow.

Flow: a financial adviser (FA) receives a claim form from a client and
forwards it to this bot. The bot runs the form-nation pipeline and returns
a filled, human-reviewed PDF.

    FA forwards PDF/photo ──► bot uploads to the form-nation API
                              (tier detection, field/area extraction)
    bot: "form received, send client details as `key: value` lines"
    FA pastes details    ──► automap (Gemini) + validators + fill
    bot sends preview image + mapping summary + ⚠ warnings
    FA taps ✅ Approve   ──► bot sends the filled PDF
           ✏️ Adjust     ──► deep link into the web UI for fine edits
           ❌ Discard    ──► session and files forgotten

Privacy note: everything sent through a Telegram bot transits Telegram's
servers (Bot API is not end-to-end encrypted). Run this only with that
understood, and keep RETENTION_HOURS short.

Run:  .venv/bin/python -m app.telegram_bot   (needs TELEGRAM_BOT_TOKEN in .env)
"""

import io
import logging
import os

import fitz
import httpx
from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Update,
)
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
    MessageHandler, filters,
)

load_dotenv()
API = os.environ.get("FORM_NATION_API", "http://127.0.0.1:8477")
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("form-nation-bot")

# chat_id -> session; one active form per chat, forgotten on completion
SESSIONS: dict[int, dict] = {}

HELP = (
    "Send me a claim form (PDF or a clear photo) that your client sent you.\n"
    "I'll detect its fields, then ask you for the client's details.\n\n"
    "After that I show you a preview — nothing is final until you approve it. "
    "Signature fields are always left for the client."
)


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 form-nation bot.\n\n" + HELP)


def _api() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=API, timeout=300)


async def _ingest(update: Update, pdf_bytes: bytes, filename: str):
    """Upload a form to the pipeline and prompt for client details."""
    chat_id = update.effective_chat.id
    msg = await update.message.reply_text("📄 Analysing the form…")
    async with _api() as api:
        r = await api.post("/api/upload",
                           files={"file": (filename, pdf_bytes,
                                           "application/pdf")})
    if r.status_code != 200:
        await msg.edit_text(f"❌ Could not process that file: "
                            f"{r.json().get('detail', r.status_code)}")
        return
    doc = r.json()
    SESSIONS[chat_id] = {"doc": doc, "values": {}}
    tiers = {1: "fillable PDF — exact fields",
             2: "flattened PDF — detected areas",
             3: "scan/photo — OCR + detected areas"}
    await msg.edit_text(
        f"✅ Form received: {doc['filename']}\n"
        f"• {len(doc['pages'])} page(s), {len(doc['fields'])} field(s)\n"
        f"• Type: {tiers.get(doc['tier'], '?')}\n\n"
        "Now send the client's details, one per line, like:\n\n"
        "name: Tan Ah Kow\n"
        "nric: S1234567D\n"
        "policy_no: PA-0012345\n"
        "address: Blk 123 Bedok North Ave 1\n"
        "phone: 91234567\n"
        "email: ahkow@example.com\n"
        "date_of_birth: 01/02/1980"
    )


async def handle_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if doc.file_size and doc.file_size > 20 * 1024 * 1024:
        await update.message.reply_text("❌ File too large (max 20 MB).")
        return
    tg_file = await doc.get_file()
    data = bytes(await tg_file.download_as_bytearray())
    name = doc.file_name or "form.pdf"
    if not name.lower().endswith(".pdf"):
        await update.message.reply_text(
            "Please send a PDF, or send the form as a photo.")
        return
    await _ingest(update, data, name)


async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Photos become a single-page image PDF -> Tier 3 pipeline."""
    photo = update.message.photo[-1]  # largest size
    tg_file = await photo.get_file()
    img_bytes = bytes(await tg_file.download_as_bytearray())
    pdf = fitz.open()
    img = fitz.open(stream=img_bytes, filetype="jpg")
    rect = img[0].rect
    page = pdf.new_page(width=rect.width, height=rect.height)
    page.insert_image(rect, stream=img_bytes)
    await _ingest(update, pdf.tobytes(), "photo-form.pdf")


def parse_profile(text: str) -> dict[str, str]:
    profile = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower().replace(" ", "_")
        if key and value.strip():
            profile[key] = value.strip()
    return profile


async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    session = SESSIONS.get(chat_id)
    if not session:
        await update.message.reply_text(
            "Send me a claim form first (PDF or photo).\n\n" + HELP)
        return
    profile = parse_profile(update.message.text)
    if not profile:
        await update.message.reply_text(
            "I couldn't read any details. Use one `key: value` per line.")
        return

    doc = session["doc"]
    msg = await update.message.reply_text(
        "🤖 Mapping details onto the form…")
    async with _api() as api:
        r = await api.post(f"/api/automap/{doc['doc_id']}",
                           json={"profile": profile})
        result = r.json()
        suggestions = result.get("suggestions", {})
        values = {fid: s["value"] for fid, s in suggestions.items()}
        v = await api.post(f"/api/validate/{doc['doc_id']}",
                           json={"values": values})
        issues = v.json().get("issues", [])
        f = await api.post(f"/api/fill/{doc['doc_id']}",
                           json={"values": values})
        filled_pdf = f.content

    session["values"] = values
    session["filled"] = filled_pdf
    session["profile"] = profile

    # preview: render filled pages that actually contain values
    filled_doc = fitz.open(stream=filled_pdf, filetype="pdf")
    pages_used = sorted({s_field_page(doc, fid) for fid in values})[:3]
    previews = []
    for pno in pages_used:
        pix = filled_doc[pno].get_pixmap(matrix=fitz.Matrix(2, 2))
        previews.append(InputMediaPhoto(io.BytesIO(pix.tobytes("png"))))
    filled_doc.close()

    names = {str(f_["id"]): f_["name"] for f_ in doc["fields"]}
    lines = [f"• {s['source_key']} → {names.get(fid, fid)[:40]}"
             f" ({int(s['confidence'] * 100)}%)"
             for fid, s in suggestions.items()]
    unmapped = [k for k in profile
                if k not in {s["source_key"] for s in suggestions.values()}]
    summary = (f"Mapped {len(suggestions)} field(s) via {result['engine']}:\n"
               + "\n".join(lines[:20]))
    if unmapped:
        summary += f"\n\n🖐 Not placed (do manually): {', '.join(unmapped)}"
    if issues:
        summary += "\n\n⚠️ Validation warnings:\n" + "\n".join(
            f"• {i['field_name'][:40]}: {i['message']}" for i in issues)
    summary += "\n\nCheck the preview, then choose:"

    if previews:
        await update.message.reply_media_group(previews)
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve & get PDF", callback_data="approve"),
        InlineKeyboardButton("❌ Discard", callback_data="discard"),
    ], [
        InlineKeyboardButton(
            "✏️ Fine-tune in web UI",
            url=f"{API}/?doc={doc['doc_id']}"),
    ]])
    await msg.edit_text(summary, reply_markup=keyboard)


def s_field_page(doc: dict, fid: str) -> int:
    for f in doc["fields"]:
        if str(f["id"]) == str(fid):
            return f["page"]
    return 0


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    session = SESSIONS.get(chat_id)
    if not session:
        await query.edit_message_text("Session expired — send the form again.")
        return
    if query.data == "approve":
        await query.message.reply_document(
            document=io.BytesIO(session["filled"]),
            filename="filled-" + session["doc"]["filename"],
            caption="Here you go. Remember: the client still signs it "
                    "themselves — signature fields were left empty.")
        await query.edit_message_text(query.message.text + "\n\n✅ Delivered.")
        SESSIONS.pop(chat_id, None)
    elif query.data == "discard":
        SESSIONS.pop(chat_id, None)
        await query.edit_message_text("🗑 Discarded. Send a new form anytime.")


def main():
    if not TOKEN:
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN not set. Create a bot with @BotFather on "
            "Telegram, then add TELEGRAM_BOT_TOKEN=... to .env")
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,
                                   handle_text))
    app.add_handler(CallbackQueryHandler(on_button))
    log.info("form-nation bot polling…")
    app.run_polling()


if __name__ == "__main__":
    main()
