import os, re, logging, threading, asyncio, hashlib, random
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from io import BytesIO

import pytz, requests
from bs4 import BeautifulSoup
import google.generativeai as genai
from supabase import create_client, Client

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from telegram.constants import ParseMode

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

# ─── Scraping ────────────────────────────────────────────────────────────────
def fetch_tweets(user, num=3):
    instance = random.choice(NITTER_INSTANCES)
    try:
        h = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/122.0.0.0"}
        r = requests.get(f"{instance}/{user}", headers=h, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")
        res = []
        for it in soup.select(".timeline-item")[:num]:
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
    check = supabase.table("noticias").select("id").eq("identificador_ia", tid).execute()
    if check.data: return False # Retornamos False si ya existe

    tipo = gemini_model.generate_content(f"Di 'fichaje' o 'noticia': {t['texto'][:150]}").text.strip().lower()
    prompt = f"Redacta para Telegram (Markdown) este {tipo}: {t['texto']}. Fuente: @{t['user']}"
    redac = gemini_model.generate_content(prompt).text.strip()
    
    supabase.table("noticias").insert({"identificador_ia": tid, "url_origen": t["url"], "tipo": tipo, "estado": "pendiente", "texto_final": redac}).execute()
    
    img_b = requests.get(t["img"]).content if t["img"] else None
    pendientes[tid] = {"texto": redac, "foto": img_b}
    
    btn = InlineKeyboardMarkup([[InlineKeyboardButton("✅ PUBLICAR", callback_data=f"p:{tid}"), InlineKeyboardButton("🗑 BORRAR", callback_data=f"d:{tid}")]])
    cap = f"🆔 `{tid}`\n\n{redac}"
    if img_b: await context.bot.send_photo(ADMIN_ID, BytesIO(img_b), caption=cap[:1024], parse_mode=ParseMode.MARKDOWN, reply_markup=btn)
    else: await context.bot.send_message(ADMIN_ID, cap, parse_mode=ParseMode.MARKDOWN, reply_markup=btn)
    return True # Retornamos True si se procesó correctamente

async def monitoreo_wrapper(context: ContextTypes.DEFAULT_TYPE):
    profundo = context.job.data if context.job and context.job.data else False
    num = 10 if profundo else 3
    encontrados = 0
    
    logging.info(f"--- Iniciando monitoreo (Profundo: {profundo}) ---")
    for c in CUENTAS_X:
        tweets = fetch_tweets(c, num)
        for t in tweets:
            fue_procesado = await procesar_tweet(t, context)
            if fue_procesado: encontrados += 1
            await asyncio.sleep(random.randint(3, 6))
        await asyncio.sleep(4)
    
    # Si después de recorrer todo, no hubo nada nuevo
    if encontrados == 0:
        await context.bot.send_message(ADMIN_ID, "📭 No se encontró nada nuevo en las cuentas de X.")

# ─── Comandos ────────────────────────────────────────────────────────────────
async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    profundo = True if (context.args and context.args[0] == "2h") else False
    msg = "🔎 Escaneo profundo (2h) iniciado..." if profundo else "🔎 Escaneo normal iniciado..."
    await update.message.reply_text(msg)
    context.job_queue.run_once(monitoreo_wrapper, when=0, data=profundo)

async def cmd_estado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    await update.message.reply_text(f"✅ Online - Pendientes: {len(pendientes)}")

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    act, tid = q.data.split(":")
    if tid in pendientes and act == "p":
        d = pendientes[tid]
        if d["foto"]: await context.bot.send_photo(CHANNEL_ID, BytesIO(d["foto"]), caption=d["texto"][:1024], parse_mode=ParseMode.MARKDOWN)
        else: await context.bot.send_message(CHANNEL_ID, d["texto"], parse_mode=ParseMode.MARKDOWN)
        supabase.table("noticias").update({"estado": "publicado"}).eq("identificador_ia", tid).execute()
    if tid in pendientes: del pendientes[tid]
    await q.edit_message_reply_markup(None)

# ─── Main ───────────────────────────────────────────────────────────────────
def main():
    threading.Thread(target=run_http_server, daemon=True).start()
    app = Application.builder().token(TOKEN).build()
    
    app.add_handler(CommandHandler("scan", cmd_scan))
    app.add_handler(CommandHandler("estado", cmd_estado))
    app.add_handler(CallbackQueryHandler(handle_callback))
    
    job_queue = app.job_queue
    # Escaneo automático cada 15 min
    job_queue.run_repeating(monitoreo_wrapper, interval=900, first=10)
    
    app.run_polling()

if __name__ == "__main__": 
    main()
