import os, re, json, logging, threading, asyncio, hashlib, random
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from io import BytesIO

import pytz, requests, feedparser
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

# ─── Servidor Keep-Alive (Para que Render no lo apague) ──────────────────────
class RenderKeepAlive(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Universo Football esta vivo")
    def log_message(self, *args): pass # Silenciar logs de consola

def run_http_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), RenderKeepAlive)
    server.serve_forever()

# ─── Lógica de Scraping y Procesamiento ──────────────────────────────────────
def fetch_tweets(user):
    instance = random.choice(NITTER_INSTANCES)
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/122.0.0.0"}
        r = requests.get(f"{instance}/{user}", headers=headers, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")
        res = []
        for it in soup.select(".timeline-item")[:3]:
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

async def procesar_tweet(t, app):
    tid = hashlib.md5(t["texto"].encode()).hexdigest()[:12]
    if supabase.table("noticias").select("id").eq("identificador_ia", tid).execute().data: return

    tipo = gemini_model.generate_content(f"Di 'fichaje' o 'noticia': {t['texto'][:150]}").text.strip().lower()
    redac = gemini_model.generate_content(f"Redacta para Telegram este {tipo}: {t['texto']}. Fuente: @{t['user']}").text.strip()
    
    supabase.table("noticias").insert({"identificador_ia": tid, "url_origen": t["url"], "tipo": tipo, "estado": "pendiente", "texto_final": redac}).execute()
    
    img_b = requests.get(t["img"]).content if t["img"] else None
    pendientes[tid] = {"texto": redac, "foto": img_b}
    
    btn = InlineKeyboardMarkup([[InlineKeyboardButton("✅ PUBLICAR", callback_data=f"p:{tid}"), InlineKeyboardButton("🗑 BORRAR", callback_data=f"d:{tid}")]])
    if img_b: await app.bot.send_photo(ADMIN_ID, BytesIO(img_b), caption=f"🆔 `{tid}`\n\n{redac}"[:1024], parse_mode="Markdown", reply_markup=btn)
    else: await app.bot.send_message(ADMIN_ID, f"🆔 `{tid}`\n\n{redac}", parse_mode="Markdown", reply_markup=btn)

# ─── Tareas y Handlers ───────────────────────────────────────────────────────
async def monitoreo(app):
    for c in CUENTAS_X:
        for t in fetch_tweets(c):
            await procesar_tweet(t, app)
            await asyncio.sleep(random.randint(5, 10))
        await asyncio.sleep(5)

async def handle_callback(update, context):
    q = update.callback_query
    await q.answer()
    act, tid = q.data.split(":")
    if tid in pendientes and act == "p":
        d = pendientes[tid]
        if d["foto"]: await context.bot.send_photo(CHANNEL_ID, BytesIO(d["foto"]), caption=d["texto"][:1024], parse_mode="Markdown")
        else: await context.bot.send_message(CHANNEL_ID, d["texto"], parse_mode="Markdown")
        supabase.table("noticias").update({"estado": "publicado"}).eq("identificador_ia", tid).execute()
    if tid in pendientes: del pendientes[tid]
    await q.edit_message_reply_markup(None)

async def post_init(app):
    sch = AsyncIOScheduler(timezone=VE_TZ)
    sch.add_job(monitoreo, "interval", minutes=15, args=[app])
    sch.start()
    await app.bot.send_message(ADMIN_ID, "asere activo")

# ─── Ejecución ───────────────────────────────────────────────────────────────
def main():
    threading.Thread(target=run_http_server, daemon=True).start() # <--- EL SALVAVIDAS
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("scan", lambda u, c: monitoreo(c.application)))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.run_polling()

if __name__ == "__main__": main()
