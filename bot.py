import os, hashlib, requests, logging, threading, asyncio, random, re
from io import BytesIO
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler

from groq import Groq
from supabase import create_client, Client
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from telegram.constants import ParseMode
from bs4 import BeautifulSoup
import feedparser

# ─── Configuración ──────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("universo_football")

TOKEN          = os.environ.get("TELEGRAM_BOT_TOKEN")
ADMIN_ID       = int(os.environ.get("ADMIN_TELEGRAM_ID", 0))
CHANNEL_ID     = os.environ.get("TELEGRAM_CHANNEL_ID")
SUPABASE_URL   = os.environ.get("SUPABASE_URL")
SUPABASE_KEY   = os.environ.get("SUPABASE_KEY")
GROQ_API_KEY   = os.environ.get("GROQ_API_KEY")

client_groq = Groq(api_key=GROQ_API_KEY)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

CUENTAS_X = ["mercatosphera", "Mercado_Ingles", "SoyCalcio_", "postunited", "laligaa_neews"]
pendientes = {}
esperando_foto = {}
esperando_hora = {}

# ─── Servidor Keep-Alive ─────────────────────────────────────────────────────
class RenderKeepAlive(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers()
        self.wfile.write(b"Universo Football Bot Active")
    def log_message(self, *args): pass

def run_http_server():
    port = int(os.environ.get("PORT", 8080))
    HTTPServer(("0.0.0.0", port), RenderKeepAlive).serve_forever()

# ─── Obtención de RSS ────────────────────────────────────────────────────────
def fetch_tweets_rss(user, num=5):
    instancias = [f"https://nitter.net/{user}/rss", f"https://xcancel.com/{user}/rss", f"https://nitter.cz/{user}/rss"]
    random.shuffle(instancias)
    for url in instancias:
        try:
            feed = feedparser.parse(url, agent='Mozilla/5.0')
            if len(feed.entries) > 0:
                res = []
                for entry in feed.entries[:num]:
                    soup = BeautifulSoup(entry.get('description', ''), "html.parser")
                    texto = soup.get_text(strip=True)
                    if not texto or "rss reader" in texto.lower(): continue
                    img = soup.find('img')['src'] if soup.find('img') else None
                    res.append({"texto": texto, "url": entry.link, "img": img, "user": user})
                if res: return res
        except: continue
    return []

# ─── Lógica de Procesamiento (PROMPT BALANCEADO) ─────────────────────────────
async def procesar_noticia(n, context):
    tid = hashlib.md5(n["texto"].encode()).hexdigest()[:12]
    try:
        if supabase.table("noticias").select("id").eq("identificador_ia", tid).execute().data: return False
    except: return False

    try:
        completion = client_groq.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": (
                    "Eres el redactor de 'Universo Football'. Estilo visual limpio y profesional.\n\n"
                    "FORMATO HTML REQUERIDO:\n"
                    "1. Emojis + Titular en negrita <b>...</b>\n"
                    "2. UN ESPACIO EN BLANCO (Salto de línea doble).\n"
                    "3. Hecho 1 (Emoji al inicio, máx 2 líneas).\n"
                    "4. Hecho 2 (Emoji al inicio, máx 2 líneas).\n"
                    "5. UN ESPACIO EN BLANCO (Salto de línea doble).\n"
                    "6. <b>ℹ️ » [Fuente]</b> (En negrita, solo si existe).\n"
                    "7. UN ESPACIO EN BLANCO (Salto de línea doble).\n"
                    "8. 📲 <b>Suscríbete en t.me/iUniversoFootball</b> (En negrita).\n\n"
                    "REGLAS:\n"
                    "- Los hechos deben estar pegados entre sí.\n"
                    "- Usa saltos de línea dobles solo para separar el Titular, el Bloque de hechos, la Fuente y la Firma.\n"
                    "- Temperatura mínima para precisión total."
                )},
                {"role": "user", "content": f"Redacta esta noticia para Telegram respetando los espacios: {n['texto']}"}
            ],
            temperature=0.1
        )
        redac = completion.choices[0].message.content.strip()
    except:
        redac = f"📢 <b>NOTICIA</b>\n\n{n['texto']}\n\n📲 <b>Suscríbete en t.me/iUniversoFootball</b>"

    try:
        supabase.table("noticias").insert({"identificador_ia": tid, "url_origen": n["url"], "estado": "pendiente", "texto_final": redac}).execute()
        img_b = None
        if n["img"]:
            try:
                r = requests.get(n["img"], timeout=10)
                if r.status_code == 200: img_b = r.content
            except: pass

        pendientes[tid] = {"texto": redac, "foto": img_b}
        btn = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ PUBLICAR", callback_data=f"p:{tid}"), InlineKeyboardButton("⏰ PROGRAMAR", callback_data=f"s:{tid}")],
            [InlineKeyboardButton("🖼 IMG", callback_data=f"f:{tid}"), InlineKeyboardButton("🗑 BORRAR", callback_data=f"d:{tid}")]
        ])
        cap = f"🆔 <code>{tid}</code>\n\n{redac}"
        if img_b: await context.bot.send_photo(ADMIN_ID, BytesIO(img_b), caption=cap, parse_mode=ParseMode.HTML, reply_markup=btn)
        else: await context.bot.send_message(ADMIN_ID, cap, parse_mode=ParseMode.HTML, reply_markup=btn)
        return True
    except: return False

# ─── Handlers (Publicar / Programar) ──────────────────────────────────────────
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    act, tid = q.data.split(":")
    if tid not in pendientes: return

    if act == "p":
        await publicar_ahora(tid, context)
        await q.edit_message_reply_markup(None)
    elif act == "s":
        esperando_hora[ADMIN_ID] = tid
        await context.bot.send_message(ADMIN_ID, "⏰ Hora (24h, ej: 21:00):")
    elif act == "d":
        del pendientes[tid]; await q.delete_message()
    elif act == "f":
        esperando_foto[ADMIN_ID] = tid
        await context.bot.send_message(ADMIN_ID, "📸 Pasa la foto:")

async def publicar_ahora(tid, context):
    d = pendientes.get(tid)
    if not d: return
    try:
        if d["foto"]: await context.bot.send_photo(CHANNEL_ID, BytesIO(d["foto"]), caption=d["texto"], parse_mode=ParseMode.HTML)
        else: await context.bot.send_message(CHANNEL_ID, d["texto"], parse_mode=ParseMode.HTML)
        supabase.table("noticias").update({"estado": "publicado"}).eq("identificador_ia", tid).execute()
        del pendientes[tid]
    except Exception as e: logger.error(f"Error: {e}")

async def recibir_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid != ADMIN_ID: return
    if uid in esperando_hora:
        tid, hora_str = esperando_hora.pop(uid), update.message.text
        try:
            h, m = map(int, hora_str.split(":"))
            ahora = datetime.now()
            prog = ahora.replace(hour=h, minute=m, second=0, microsecond=0)
            delay = (prog - ahora).total_seconds()
            if delay < 0: delay += 86400
            context.job_queue.run_once(lambda ctx: publicar_ahora(tid, ctx), when=delay)
            await update.message.reply_text(f"✅ Programado para las {hora_str}")
        except: await update.message.reply_text("❌ Formato HH:MM")
    elif uid in esperando_foto:
        tid = esperando_foto.pop(uid)
        if update.message.photo:
            foto = await update.message.photo[-1].get_file()
            f_byte = await foto.download_as_bytearray()
            if tid in pendientes:
                pendientes[tid]["foto"] = bytes(f_byte)
                await update.message.reply_text("✅ Foto OK")

# ─── Main ───────────────────────────────────────────────────────────────────
async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id == ADMIN_ID:
        await update.message.reply_text("🔎 Escaneando..."); context.job_queue.run_once(monitoreo_wrapper, when=0)

async def monitoreo_wrapper(context: ContextTypes.DEFAULT_TYPE):
    for c in CUENTAS_X:
        items = fetch_tweets_rss(c)
        for item in items:
            if await procesar_noticia(item, context): await asyncio.sleep(2)

def main():
    threading.Thread(target=run_http_server, daemon=True).start()
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("scan", cmd_scan))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_input))
    app.add_handler(MessageHandler(filters.PHOTO, recibir_input))
    app.job_queue.run_repeating(monitoreo_wrapper, interval=900, first=10)
    app.run_polling()

if __name__ == "__main__": 
    main()
