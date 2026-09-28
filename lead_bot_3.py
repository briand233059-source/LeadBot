import os, io, re, csv, logging
from datetime import datetime
from dotenv import load_dotenv
load_dotenv()
from telegram import Update, InputFile
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

user_sessions: dict[int, dict] = {}

def get_session(uid):
    if uid not in user_sessions:
        user_sessions[uid] = {"leads": [], "seen_keys": set()}
    return user_sessions[uid]

def get_leads(uid): return get_session(uid)["leads"]
def clear_session(uid): user_sessions[uid] = {"leads": [], "seen_keys": set()}

def dedup_key(lead: dict) -> str:
    email = (lead.get("email") or "").strip().lower()
    phone = re.sub(r"[^\d]", "", lead.get("phone") or "")
    if email:   return f"email:{email}"
    if phone:   return f"phone:{phone}"
    name = (lead.get("name") or "").strip().lower()
    if name:    return f"name:{name}"
    return f"raw:{lead}"

def add_leads(uid: int, new_leads: list[dict]) -> tuple[int, int]:
    session = get_session(uid)
    added, skipped = 0, 0
    for lead in new_leads:
        key = dedup_key(lead)
        if key in session["seen_keys"]:
            skipped += 1
        else:
            session["seen_keys"].add(key)
            session["leads"].append(lead)
            added += 1
    return added, skipped

METADATA_NOISE = re.compile(
    r"""
    (?:
        (?:request\s*speed|response\s*time|load\s*time|page\s*speed|
           speed|duration|time|latency|ping|ms|ttfb)
        \s*[:\-]\s*
        [\d.,]+\s*(?:ms|s|sec|seconds|min)?\s*[|\-–—]?\s*
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)

LEFTOVER_SEPARATORS = re.compile(r"^\s*[|\-–—]+\s*|\s*[|\-–—]+\s*$")

def clean_comment(text: str) -> str:
    text = METADATA_NOISE.sub("", text)
    text = LEFTOVER_SEPARATORS.sub("", text)
    return text.strip()

KNOWN_LABEL_PATTERN = re.compile(
    r"^(name|phone|mobile|cell|tel|email|e-mail|car|vehicle|price|car price|url|link|subject|interested in)",
    re.IGNORECASE
)

def extract(text, *patterns):
    for p in patterns:
        m = re.search(p, text, re.IGNORECASE | re.MULTILINE)
        if m: return m.group(1).strip()
    return ""

def parse_lead(block: str) -> dict | None:
    block = block.strip()
    if not block: return None

    name  = extract(block, r"^Name[:\s]+(.+)$")
    phone = extract(block, r"^(?:Phone|Mobile|Cell|Tel)[:\s]+(.+)$",
                           r"(\+?1?\s*[\(]?\d{3}[\).\-\s]?\d{3}[\-.\s]?\d{4})")
    if phone: phone = re.sub(r"[^\d\+\(\)\-\s]", "", phone).strip()

    email = extract(block, r"^E-?mail[:\s]+(.+)$", r"([\w.\-+]+@[\w\-]+\.[\w.]+)")

    car   = extract(block, r"^Car[:\s]+(.+)$", r"^Vehicle[:\s]+(.+)$",
                           r"I'?m interested in[:\s]*(.+)")
    car   = re.sub(r"\s*https?://\S+", "", car).strip()

    price = extract(block, r"^Car\s*price[:\s]+(.+)$", r"^Price[:\s]+(.+)$",
                           r"(\$[\d,]+(?:\.\d{2})?)")

    url   = extract(block, r"^(?:Url|URL|Link)[:\s]+(https?://\S+)$")
    if not url:
        urls = re.findall(r"https?://\S+", block)
        if urls: url = urls[-1]

    comment_lines = []
    for line in block.splitlines():
        line = line.strip()
        if not line: continue
        if KNOWN_LABEL_PATTERN.match(line): continue
        if re.match(r"^https?://\S+$", line): continue
        if re.match(r"^[\+\d\s\(\)\-\.]{7,}$", line): continue
        if re.match(r"^[\w.\-+]+@[\w\-]+\.[\w.]+$", line): continue
        if re.match(r"^[\U0001F300-\U0001FAFF\s]+$", line): continue
        cleaned = clean_comment(line)
        if cleaned:
            comment_lines.append(cleaned)

    comments = " | ".join(comment_lines) if comment_lines else ""

    if any([name, email, phone]):
        return {"name": name, "phone": phone, "email": email,
                "car": car, "price": price, "url": url, "comments": comments}
    return None

def parse_all(text: str) -> list[dict]:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r"\n\s*\n|\n(?=📩)|\n(?=New contact)|\n[-─═=]{3,}\n", text)
    leads = []
    for block in blocks:
        lead = parse_lead(block.strip())
        if lead: leads.append(lead)
    return leads

FIELDS  = ["name", "phone", "email", "car", "price", "url", "comments"]
HEADERS = ["Name", "Phone", "Email", "Car", "Price", "Link", "Comments"]

def sort_leads(leads: list[dict]) -> list[dict]:
    return sorted(leads, key=lambda l: (l.get("car") or "").strip().lower() or "\xff")

def build_csv(leads: list[dict]) -> str:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(HEADERS)
    for l in sort_leads(leads):
        w.writerow([l.get(f, "") for f in FIELDS])
    return out.getvalue()

def build_txt(leads: list[dict]) -> str:
    lines = []
    for l in sort_leads(leads):
        lines.append("\t".join([l.get(f, "") for f in FIELDS]))
    return "\n".join(lines)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *Lead Extractor Bot*\n\n"
        "Forward me as many lead messages as you want.\n"
        "I collect them all silently.\n\n"
        "When you're done → /export → get ONE file with everything.\n\n"
        "/count — see how many leads collected\n"
        "/clear — wipe session and start fresh\n\n"
        "📋 Export columns: Name · Phone · Email · Car · Price · Link · *Comments*\n"
        "_(Metadata like \"Request speed: 38 s |\" is automatically removed from comments)_",
        parse_mode="Markdown"
    )

async def count_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    n = len(get_leads(update.effective_user.id))
    await update.message.reply_text(
        f"📊 You have *{n}* unique lead(s) collected so far.\nSend /export when ready.",
        parse_mode="Markdown"
    )

async def clear_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    n = len(get_leads(update.effective_user.id))
    clear_session(update.effective_user.id)
    await update.message.reply_text(f"🗑️ Cleared {n} lead(s). Session is fresh.")

async def export_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid   = update.effective_user.id
    leads = get_leads(uid)
    if not leads:
        await update.message.reply_text("📭 No leads yet. Forward some leads first, then /export.")
        return

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    csv_bytes = build_csv(leads).encode("utf-8")
    await update.message.reply_document(
        document=InputFile(io.BytesIO(csv_bytes), filename=f"leads_{ts}.csv"),
        caption=(
            f"✅ *{len(leads)} leads* — CSV file\n\n"
            "Google Sheets → File → Import → Upload this file\n"
            "Columns: Name · Phone · Email · Car · Price · Link · Comments"
        ),
        parse_mode="Markdown"
    )

    txt_bytes = build_txt(leads).encode("utf-8")
    await update.message.reply_document(
        document=InputFile(io.BytesIO(txt_bytes), filename=f"leads_{ts}.txt"),
        caption="📄 TXT file — tab-separated, no header. Paste directly into Google Sheets.",
        parse_mode="Markdown"
    )

    clear_session(uid)
    await update.message.reply_text("🗑️ Session cleared — ready for new leads.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or update.message.caption or "").strip()
    if not text:
        return

    uid       = update.effective_user.id
    new_leads = parse_all(text)

    if not new_leads:
        await update.message.reply_text(
            "❌ No lead info found in this message. Make sure it has Name/Phone/Email fields."
        )
        return

    added, skipped = add_leads(uid, new_leads)
    total = len(get_leads(uid))

    msg = f"✅ *{added}* lead(s) added — *{total}* total collected.\n"
    if skipped > 0:
        msg += f"⚠️ *{skipped}* duplicate(s) skipped.\n"
    msg += "Keep forwarding or send /export when done."

    await update.message.reply_text(msg, parse_mode="Markdown")

async def on_startup(app):
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
        log.info("Webhook cleared — polling clean.")
    except Exception as e:
        log.warning("Could not clear webhook: %s", e)

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    err = context.error
    if "Conflict" in str(err):
        log.warning("⚠️  Conflict detected — another bot instance may be running.")
    else:
        log.error("Unhandled error: %s", err, exc_info=err)

def main():
    token = BOT_TOKEN.strip()
    if not token:
        print("\nERROR: No token found.\nCreate a .env file with:\nTELEGRAM_BOT_TOKEN=your_token_here\n")
        input("Press Enter to exit...")
        return

    print("\n🤖 LEAD EXTRACTOR BOT — Running")
    print("Columns: Name · Phone · Email · Car · Price · Link · Comments")
    print("Metadata noise (e.g. 'Request speed: 38 s |') stripped from comments automatically.")
    print("Forward leads → /export when done\nCtrl+C to stop\n")

    app = (
        ApplicationBuilder()
        .token(token)
        .post_init(on_startup)
        .build()
    )

    app.add_handler(CommandHandler("start",  start))
    app.add_handler(CommandHandler("help",   start))
    app.add_handler(CommandHandler("count",  count_cmd))
    app.add_handler(CommandHandler("clear",  clear_cmd))
    app.add_handler(CommandHandler("export", export_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )

if __name__ == "__main__":
    main()
