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

import asyncio
import io
import logging
import os
import re
from datetime import datetime

import fitz
import httpx
from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Update,
)
from telegram.constants import ChatAction
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
    MessageHandler, filters,
)

from app import db, llm

load_dotenv()
API = os.environ.get("FORM_NATION_API", "http://127.0.0.1:8477")
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

from pathlib import Path  # noqa: E402

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "data" / "templates"
TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("form-nation-bot")
# httpx logs full request URLs at INFO — which include the bot token
logging.getLogger("httpx").setLevel(logging.WARNING)

# chat_id -> session; one active form per chat, forgotten on completion
SESSIONS: dict[int, dict] = {}

HELP = (
    "I collect claim details as you message me, and I fill the form when "
    "you're ready.\n\n"
    "• Send the claim form (PDF or photo) whenever — before or after the "
    "details.\n"
    "• Then just tell me about the claim in normal messages, e.g.\n"
    "  \"Tan Ah Kow S1234567D fell off his bike at ECP on 28 Jun, "
    "fractured wrist, warded at CGH\"\n"
    "• After each message I show what I've captured and what's still "
    "missing.\n"
    "• Tap 📝 Fill the form when you're satisfied — you always review a "
    "preview before anything is final. Signature fields are never filled.\n\n"
    "Commands:\n"
    "/newclaim — start a claim from a registered form template\n"
    "/newtype <name> — register a claim type (then send its blank form once)\n"
    "/clients — list saved clients (auto-saved whenever you approve a claim)"
)


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 form-nation bot.\n\n" + HELP)


async def delclient_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    clients = db.list_clients()
    if not clients:
        await update.message.reply_text("No saved clients.")
        return
    rows = [[InlineKeyboardButton(f"🗑 {c['name']}",
                                  callback_data=f"delclient:{c['id']}")]
            for c in clients[:12]]
    await update.message.reply_text(
        "Tap a client to permanently delete their saved record:",
        reply_markup=InlineKeyboardMarkup(rows))


async def history_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    forms = db.form_history(10)
    if not forms:
        await update.message.reply_text("No claims processed yet.")
        return
    status_icon = {"uploaded": "📄", "mapped": "🤖", "filled": "✅"}
    lines = [
        f"{status_icon.get(f['status'], '•')} {f['filename']} — "
        f"{f['status']} ({datetime.fromtimestamp(f['created_at']):%d %b %H:%M})"
        for f in forms]
    await update.message.reply_text("Recent claims:\n" + "\n".join(lines))


async def clients_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    clients = db.list_clients()
    if not clients:
        await update.message.reply_text(
            "No saved clients yet. After you paste a client's details for a "
            "form, tap 💾 Save to keep them for reuse.")
        return
    lines = [f"• {c['name']} ({len(c['profile'])} details)" for c in clients]
    await update.message.reply_text(
        "Saved clients:\n" + "\n".join(lines) +
        "\n\nThey appear as buttons whenever you send a form.")


def _api() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=API, timeout=300)


async def _ingest(update: Update, pdf_bytes: bytes, filename: str):
    await ingest_bytes(update.effective_chat.id, update.get_bot(),
                       pdf_bytes, filename)


async def ingest_bytes(chat_id: int, bot, pdf_bytes: bytes, filename: str):
    """Upload a form to the pipeline and prompt for client details."""
    msg = await bot.send_message(chat_id, "📄 Analysing the form…")
    async with _api() as api:
        r = await api.post("/api/upload",
                           files={"file": (filename, pdf_bytes,
                                           "application/pdf")})
    if r.status_code != 200:
        await msg.edit_text(f"❌ Could not process that file: "
                            f"{r.json().get('detail', r.status_code)}")
        return
    doc = r.json()
    session = SESSIONS.setdefault(chat_id, {"profile": {}})
    session["doc"] = doc
    tiers = {1: "fillable PDF — exact fields",
             2: "flattened PDF — detected areas",
             3: "scan/photo — OCR + detected areas"}
    text = (
        f"✅ Form received: {doc['filename']}\n"
        f"• {len(doc['pages'])} page(s), {len(doc['fields'])} field(s)\n"
        f"• Type: {tiers.get(doc['tier'], '?')}\n\n"
    )
    if session["profile"]:
        text += (f"I already have {len(session['profile'])} detail(s) from "
                 "your messages. Keep sending more, or tap 📝 to fill now.")
    else:
        text += ("Now tell me about the claim in normal messages — or pick "
                 "a saved client to start from.")
    rows = []
    if session["profile"]:
        rows.append([InlineKeyboardButton("📝 Fill the form now",
                                          callback_data="fillnow")])
    rows += [[InlineKeyboardButton(f"👤 {c['name']}",
                                   callback_data=f"client:{c['id']}")]
             for c in db.list_clients()[:6]]
    keyboard = InlineKeyboardMarkup(rows) if rows else None
    await msg.edit_text(text, reply_markup=keyboard)


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
    session = SESSIONS.setdefault(update.effective_chat.id, {"profile": {}})
    if session.get("awaiting_type"):
        type_name = session.pop("awaiting_type")
        path = TEMPLATES_DIR / f"{type_name.lower().replace(' ', '-')}.pdf"
        path.write_bytes(data)
        db.save_type(type_name, str(path), name)
        await update.message.reply_text(
            f"📚 Claim type \"{type_name}\" registered.\n"
            "Start one anytime with /newclaim — no need to send this "
            "form again.")
        return
    await _ingest(update, data, name)


async def newtype_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    name = " ".join(ctx.args).strip() if ctx.args else ""
    if not name:
        await update.message.reply_text(
            "Usage: /newtype <name>\ne.g. /newtype Personal Accident\n"
            "Then send me the blank form PDF for that claim type.")
        return
    session = SESSIONS.setdefault(update.effective_chat.id, {"profile": {}})
    session["awaiting_type"] = name
    await update.message.reply_text(
        f"📎 OK — send me the blank \"{name}\" form (PDF) now.")


async def newclaim_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    types = db.list_types()
    if not types:
        await update.message.reply_text(
            "No claim types registered yet.\n"
            "Create one with /newtype <name>, then send its blank form once.")
        return
    rows = [[InlineKeyboardButton(f"📄 {t['name']}",
                                  callback_data=f"type:{t['id']}")]
            for t in types[:12]]
    await update.message.reply_text(
        "Which claim type?", reply_markup=InlineKeyboardMarkup(rows))


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
    """Conversational intake: extract facts from any message, track gaps."""
    chat_id = update.effective_chat.id
    session = SESSIONS.setdefault(chat_id, {"profile": {}})
    doc = session.get("doc")
    form_fields = ([f["name"] for f in doc["fields"]
                    if f["type"] != "Signature"] if doc else None)

    await ctx.bot.send_chat_action(chat_id, ChatAction.TYPING)
    thinking = await update.message.reply_text("🤔 Reading that…")
    try:
        result = llm.extract_details(update.message.text,
                                     session["profile"], form_fields)
    except Exception:
        # offline fallback: accept key: value lines
        kv = parse_profile(update.message.text)
        result = {"extracted": kv, "missing": []}
    new = result["extracted"]
    session["profile"].update(new)

    lines = []
    if new:
        lines.append("📥 Captured just now:")
        lines += [f"  • {k.replace('_', ' ')}: {v}" for k, v in new.items()]
    else:
        lines.append("🤷 No new details found in that message.")
    lines.append(f"\n📋 Total collected: {len(session['profile'])} detail(s)")
    if result["missing"]:
        lines.append("❓ Still missing:")
        lines += [f"  • {m}" for m in result["missing"]]
    if not doc:
        lines.append("\n📎 Send me the claim form (PDF/photo) when ready.")

    buttons = []
    if doc and session["profile"]:
        buttons.append([InlineKeyboardButton("📝 Fill the form now",
                                             callback_data="fillnow")])
    row2 = []
    if session["profile"].get("name"):
        row2.append(InlineKeyboardButton(
            f"💾 Save \"{session['profile']['name']}\"",
            callback_data="save_client"))
    row2.append(InlineKeyboardButton("🗑 Reset details",
                                     callback_data="reset"))
    buttons.append(row2)
    session["profile_dirty"] = True
    await thinking.edit_text("\n".join(lines),
                             reply_markup=InlineKeyboardMarkup(buttons))


async def run_mapping(chat_id: int, ctx: ContextTypes.DEFAULT_TYPE,
                      profile: dict[str, str], offer_save: bool = False):
    session = SESSIONS.get(chat_id)
    doc = session["doc"]
    await ctx.bot.send_chat_action(chat_id, ChatAction.UPLOAD_DOCUMENT)
    msg = await ctx.bot.send_message(chat_id,
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
        readback = None
        try:  # closed-loop check: re-read the filled form
            rb = await api.post(f"/api/verify/{doc['doc_id']}",
                                json={"values": values})
            readback = rb.json() if rb.status_code == 200 else None
        except Exception:
            pass

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
    if readback:
        summary += (f"\n\n🔍 Read-back check: {readback['matched']}/"
                    f"{readback['checked']} values verified on the page")
        for m in readback.get("mismatches", [])[:5]:
            summary += (f"\n• {m['name'][:35]}: expected "
                        f"\"{m['expected'][:25]}\", saw \"{m['seen'][:25]}\"")
    summary += "\n\nCheck the preview, then choose:"

    if previews:
        await ctx.bot.send_media_group(chat_id, previews)
    buttons = [[
        InlineKeyboardButton("✅ Approve & get PDF", callback_data="approve"),
        InlineKeyboardButton("❌ Discard", callback_data="discard"),
    ], [
        InlineKeyboardButton(
            "✏️ Fine-tune in web UI",
            url=f"{API}/?doc={doc['doc_id']}"),
    ]]
    if offer_save and profile.get("name"):
        buttons.append([InlineKeyboardButton(
            f"💾 Save \"{profile['name']}\" for reuse",
            callback_data="save_client")])
    await msg.edit_text(summary, reply_markup=InlineKeyboardMarkup(buttons))


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
    if query.data.startswith("type:"):
        ctype = db.get_type(int(query.data.split(":", 1)[1]))
        if ctype is None or not Path(ctype["path"]).exists():
            await query.message.reply_text(
                "That claim type's template is missing — re-register it "
                "with /newtype.")
            return
        await query.message.reply_text(f"📄 Starting a {ctype['name']} claim…")
        await ingest_bytes(chat_id, ctx.bot,
                           Path(ctype["path"]).read_bytes(),
                           ctype["filename"] or f"{ctype['name']}.pdf")
        return
    if query.data.startswith("delclient:"):
        client = db.get_client(int(query.data.split(":", 1)[1]))
        if client and db.delete_client(client["id"]):
            await query.edit_message_text(
                f"🗑 Deleted \"{client['name']}\" permanently.")
        else:
            await query.edit_message_text("Already deleted.")
        return
    if query.data.startswith("client:"):
        client = db.get_client(int(query.data.split(":", 1)[1]))
        if client is None:
            await query.message.reply_text("That client was deleted.")
            return
        session.setdefault("profile", {}).update(client["profile"])
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(
            "📝 Fill the form now", callback_data="fillnow")]]) \
            if session.get("doc") else None
        await query.message.reply_text(
            f"👤 Loaded {client['name']} ({len(client['profile'])} details). "
            "Send more claim details, or fill now.",
            reply_markup=keyboard)
        return
    if query.data == "fillnow":
        if not session.get("doc"):
            await query.message.reply_text(
                "📎 Send me the claim form first (PDF or photo).")
            return
        if not session.get("profile"):
            await query.message.reply_text("No details collected yet.")
            return
        await run_mapping(chat_id, ctx, session["profile"])
        return
    if query.data == "reset":
        session["profile"] = {}
        await query.message.reply_text(
            "🗑 Details cleared. Tell me about the claim from scratch.")
        return
    if query.data == "save_client":
        profile = session.get("profile") or {}
        if profile.get("name"):
            db.save_client(profile["name"], profile)
            await query.message.reply_text(
                f"💾 Saved \"{profile['name']}\" — they'll appear as a "
                "button whenever you send a form.")
        return
    if query.data == "approve":
        caption = ("Here you go. Remember: the client still signs it "
                   "themselves — signature fields were left empty.")
        profile = session.get("profile") or {}
        if profile.get("name"):  # auto-create/update the client record
            db.save_client(profile["name"], profile)
            caption += f"\n👤 Client record for {profile['name']} updated."
        who = re.sub(r"[^\w]+", "-", profile.get("name", "client")).strip("-")
        form = re.sub(r"[^\w]+", "-",
                      session["doc"]["filename"].rsplit(".", 1)[0])[:40]
        outname = f"{who}_{form}_{datetime.now():%Y-%m-%d}.pdf"
        ttl_min = float(os.environ.get("DELETE_DELIVERED_MINUTES", "60"))
        if ttl_min > 0:
            caption += (f"\n⏳ This file auto-deletes from the chat in "
                        f"{ttl_min:.0f} min — save it now.")
        sent = await query.message.reply_document(
            document=io.BytesIO(session["filled"]),
            filename=outname,
            caption=caption)
        if ttl_min > 0:  # remove the filled PDF from Telegram's servers
            async def _expire(bot=ctx.bot, cid=chat_id,
                              mid=sent.message_id, delay=ttl_min * 60):
                await asyncio.sleep(delay)
                try:
                    await bot.delete_message(cid, mid)
                except Exception:
                    pass
            asyncio.create_task(_expire())
        await query.edit_message_text(query.message.text + "\n\n✅ Delivered.")
        SESSIONS.pop(chat_id, None)
    elif query.data == "discard":
        SESSIONS.pop(chat_id, None)
        await query.edit_message_text("🗑 Discarded. Send a new form anytime.")


async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    """Never fail silently — tell the chat something went wrong."""
    log.exception("handler error", exc_info=ctx.error)
    chat = getattr(update, "effective_chat", None)
    if chat is not None:
        try:
            await ctx.bot.send_message(
                chat.id,
                "⚠️ Something went wrong on my side "
                f"({type(ctx.error).__name__}). Please try that again.")
        except Exception:
            pass


def main():
    if not TOKEN:
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN not set. Create a bot with @BotFather on "
            "Telegram, then add TELEGRAM_BOT_TOKEN=... to .env")
    app = (Application.builder().token(TOKEN)
           .connect_timeout(20).read_timeout(60)
           .write_timeout(60).media_write_timeout(120)
           .pool_timeout(20).build())
    app.add_error_handler(on_error)
    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(CommandHandler("clients", clients_cmd))
    app.add_handler(CommandHandler("newtype", newtype_cmd))
    app.add_handler(CommandHandler(["newclaim", "types"], newclaim_cmd))
    app.add_handler(CommandHandler("history", history_cmd))
    app.add_handler(CommandHandler("delclient", delclient_cmd))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,
                                   handle_text))
    app.add_handler(CallbackQueryHandler(on_button))
    log.info("form-nation bot polling…")
    app.run_polling()


if __name__ == "__main__":
    main()
