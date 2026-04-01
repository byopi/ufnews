import os, re, json, logging, threading, asyncio, hashlib, random
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from io import BytesIO

import pytz, requests
from bs4 import BeautifulSoup
import google.generativeai as genai
from supabase import create_client, Client

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from telegram.constants import ParseMode
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ─── Configuración ──────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
VE_TZ = pytz.timezone("America/Caracas")

TOKEN          = os.environ["TELEGRAM_BOT_TOKEN"]
ADMIN_ID       = int(os.environ["ADMIN_TELEGRAM_ID"])
CHANNEL_ID     = os.environ["TELEGRAM_CHANNEL_ID"]
SUPABASE_URL   = os.environ["SUPABASE_URL"]
SUPABASE_KEY   = os.environ["SUPABASE_KEY"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

genai.configure(api_key=GEMINI_API_KEY)
gemini_model = genai.GenerativeModel("gemini-1.5-flash")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

pendientes = {}
CUENTAS_X = ["mercatosphera", "Mercado_Ingles", "SoyCalcio_", "postunited"]
NITTER_INSTANCES = ["https://nitter.privacydev.net", "https://nitter.poast.org", "https://nitter.perennialte.ch"]

# ─── Servidor Keep-Alive ─────────────────────────────────────────────────────
class RenderKeepAlive(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers()
        self.wfile.write(b"Universo Football OK")
    def log_message(self, *args): pass

def run_http_server():
    port = int(os.environ.get("PORT", 8080))
    HTTPServer(("0.0.0.0", port), RenderKeepAlive).serve_forever()

# ─── Scraping y Procesamiento ────────────────────────────────────────────────
def fetch_tweets(user, num_tweets=3):
    instance = random.choice(NITTER_INSTANCES)
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/122.0.0.0"}
        r = requests.get(f"{instance}/{user}", headers=headers, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")
        res = []
        # Si pedimos "2h", aumentamos el rango de búsqueda en el feed
        for it in soup.select(".timeline-item")[:num_tweets]:
            txt = it.select_one(".tweet-content")
            if not txt: continue
            lnk = it.select_one(".tweet-link")
            img = it.select_one(".attachment img")
            res.append({
                "texto": txt.get_text(strip=True),
                "url": f"https://x.com{lnk['href']}" if lnk else instance,
                "img": f"{instance}{img['src']}" if img else None,
                "user": user
            })
        return res
    except: return []

async def procesar_tweet(t, context):
    tid = hashlib.md5(t["texto"].encode()).hexdigest()[:12]
    if supabase.table("noticias").select("id").eq("identificador_ia", tid).execute().data: return

    tipo = gemini_model.generate_content(f"Di 'fichaje' o 'noticia': {t['texto'][:150]}").text.strip().lower()
    redac = gemini_model.generate_content(f"Redacta para Telegram este {tipo}: {t['texto']}. Fuente: @{t['user']}").text.strip()
    
    supabase.table("noticias").insert({"identificador_ia": tid, "url_origen": t["url"], "tipo": tipo, "estado": "pendiente", "texto_final": redac}).execute()
    
    img_b = requests.get(t["img"]).content if t["img"] else None
    pendientes[tid] = {"texto": redac, "foto": img_b}
    
    btn = InlineKeyboardMarkup([[InlineKeyboardButton("✅ PUBLICAR", callback_data=f"p:{tid}"), InlineKeyboardButton("🗑 BORRAR", callback_data=f"d:{tid}")]])
    if img_b: await context.bot.send_photo(ADMIN_ID, BytesIO(img_b), caption=f"🆔 `{tid}`\n\n{redac}"[:1024], parse_mode="Markdown", reply_markup=btn)
    else: await context.bot.send_message(ADMIN_ID, f"🆔 `{tid}`\n\n{redac}", parse_mode="Markdown", reply_markup=btn)

# ─── Tareas ──────────────────────────────────────────────────────────────────
async def monitoreo(context, profundo=False):
    # Si es profundo (2h), buscamos los últimos 10 tweets, si no, solo 3.
    cantidad = 10 if profundo else 3
    logging.info(f"--- Iniciando monitoreo (Profundo: {profundo}) ---")
    for c in CUENTAS_X:
        tweets = fetch_tweets(c, cantidad)
        for t in tweets:
            await procesar_tweet(t, context)
            await asyncio.sleep(random.randint(3, 6))
        await asyncio.sleep(4)

# ─── Handlers de Comandos ────────────────────────────────────────────────────
async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    
    profundo = False
    msg = "🔎 Escaneando noticias recientes..."
    
    if context.args and context.args[0].lower() == "2h":
        profundo = True
        msg = "🔎 *Escaneando últimas 2 horas* (Búsqueda profunda)..."
    
    await update.message.reply_text(msg, parse_mode="Markdown")
    asyncio.create_task(monitoreo(context, profundo))

async def cmd_estado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    await update.message.reply_text(f"✅ *Bot Online*\nEsperando aprobación: `{len(pendientes)}`", parse_mode="Markdown")

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id != ADMIN_ID: return
    await q.answer()
    act, tid = q.data.split(":")
    if tid in pendientes and act == "p":
        d = pendientes[tid]
        if d["foto"]: await context.bot.send_photo(CHANNEL_ID, BytesIO(d["foto"]), caption=d["texto"][:1024], parse_mode="Markdown")
        else: await context.bot.send_message(CHANNEL_ID, d["texto"], parse_mode="Markdown")
        supabase.table("noticias").update({"estado": "publicado"}).eq("identificador_ia", tid).execute()
    if tid in pendientes: del pendientes[tid]
    await q.edit_message_reply_markup(None)

async def post_init(app: Application):
    sch = AsyncIOScheduler(timezone=VE_TZ)
    # Tarea automática normal cada 15 min
    sch.add_job(lambda: asyncio.run_coroutine_threadsafe(monitoreo(ContextTypes.DEFAULT_TYPE(app)), asyncio.get_running_loop()), "interval", minutes=15)
    sch.start()
    await app.bot.send_message(ADMIN_ID,"🟢 *Bot iniciado asere*\n\n"
                "/estado — Estado del bot\n"
                "/pendientes — Noticias en espera\n"
                "/scan — Forzar escaneo ahora"
                "/scan 'N°h' — Fuerza el escaneo desde publicaciones de horas anteriores")

# ─── Main ───────────────────────────────────────────────────────────────────
def main():
    threading.Thread(target=run_http_server, daemon=True).start()
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("scan", cmd_scan))
    app.add_handler(CommandHandler("estado", cmd_estado))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.run_polling()

if __name__ == "__main__": 
    main()
