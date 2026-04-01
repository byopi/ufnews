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
logger = logging.getLogger("universo_football")
VE_TZ = pytz.timezone("America/Caracas")

TOKEN          = os.environ["TELEGRAM_BOT_TOKEN"]
ADMIN_ID       = int(os.environ["ADMIN_ID"])
CHANNEL_ID     = os.environ["CHANNEL_ID"]
SUPABASE_URL   = os.environ["SUPABASE_URL"]
SUPABASE_KEY   = os.environ["SUPABASE_KEY"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

genai.configure(api_key=GEMINI_API_KEY)
gemini_model = genai.GenerativeModel("gemini-1.5-flash")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

pendientes = {}
CUENTAS_X = ["mercatosphera", "Mercado_Ingles", "SoyCalcio_", "postunited"]

# Priorizamos xcancel.com y mantenemos alternativas por si acaso
INSTANCIAS = [
    "https://xcancel.com",
    "https://nitter.net",
    "https://nitter.privacydev.net",
    "https://nitter.perennialte.ch"
]

# ─── Servidor Keep-Alive ─────────────────────────────────────────────────────
class RenderKeepAlive(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers()
        self.wfile.write(b"Universo Football OK")
    def log_message(self, *args): pass

def run_http_server():
    port = int(os.environ.get("PORT", 8080))
    HTTPServer(("0.0.0.0", port), RenderKeepAlive).serve_forever()

# ─── Scraping Multi-Instancia ────────────────────────────────────────────────
def fetch_tweets(user, num=3):
    # Intentamos primero con xcancel.com por ser tu sugerencia
    instancias_ordenadas = ["https://xcancel.com"] + [i for i in INSTANCIAS if i != "https://xcancel.com"]
    
    for instance in instancias_ordenadas:
        logger.info(f"Probando {instance}/{user}...")
        try:
            h = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
            r = requests.get(f"{instance}/{user}", headers=h, timeout=15)
            
            if r.status_code != 200: continue

            soup = BeautifulSoup(r.text, "html.parser")
            # xcancel usa las mismas clases que nitter usualmente
            items = soup.select(".timeline-item")
            
            if not items: continue
            
            res = []
            for it in items[:num]:
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
            
            if res:
                logger.info(f"✅ Éxito con {instance} ({len(res)} tweets)")
                return res
        except Exception as e:
            logger.error(f"Error en {instance}: {e}")
            continue
            
    return []

# ─── Procesamiento ───────────────────────────────────────────────────────────
async def procesar_tweet(t, context):
    tid = hashlib.md5(t["texto"].encode()).hexdigest()[:12]
    # Filtro de duplicados (Supabase)
    if supabase.table("noticias").select("id").eq("identificador_ia", tid).execute().data:
        return False

    try:
        tipo = gemini_model.generate_content(f"Responde 'fichaje' o 'noticia': {t['texto'][:150]}").text.strip().lower()
        redac = gemini_model.generate_content(f"Redacta para Telegram (Markdown) este {tipo}: {t['texto']}. Fuente: @{t['user']}").text.strip()
        
        supabase.table("noticias").insert({"identificador_ia": tid, "url_origen": t["url"], "tipo": tipo, "estado": "pendiente", "texto_final": redac}).execute()
        
        img_b = None
        if t["img"]:
            try: img_b = requests.get(t["img"], timeout=8).content
            except: pass

        pendientes[tid] = {"texto": redac, "foto": img_b}
        btn = InlineKeyboardMarkup([[InlineKeyboardButton("✅ PUBLICAR", callback_data=f"p:{tid}"), InlineKeyboardButton("🗑 BORRAR", callback_data=f"d:{tid}")]])
        
        cap = f"🆔 `{tid}`\n\n{redac}"
        if img_b: await context.bot.send_photo(ADMIN_ID, BytesIO(img_b), caption=cap[:1024], parse_mode=ParseMode.MARKDOWN, reply_markup=btn)
        else: await context.bot.send_message(ADMIN_ID, cap, parse_mode=ParseMode.MARKDOWN, reply_markup=btn)
        return True
    except: return False

async def monitoreo_wrapper(context: ContextTypes.DEFAULT_TYPE):
    profundo = context.job.data if context.job and context.job.data else False
    num = 12 if profundo else 4
    encontrados = 0
    
    logger.info(f"--- Iniciando monitoreo (Profundo: {profundo}) ---")
    for c in CUENTAS_X:
        tweets = fetch_tweets(c, num)
        for t in tweets:
            if await procesar_tweet(t, context): encontrados += 1
            await asyncio.sleep(1)
        await asyncio.sleep(2)
    
    if encontrados == 0:
        await context.bot.send_message(ADMIN_ID, "📭 Escaneo listo. No hay nada nuevo que no esté ya en la base de datos.")

# ─── Handlers y Main ─────────────────────────────────────────────────────────
async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    profundo = True if (context.args and context.args[0] == "2h") else False
    await update.message.reply_text(f"🔎 Escaneando {'(2h)' if profundo else 'recientes'} vía xcancel...")
    context.job_queue.run_once(monitoreo_wrapper, when=0, data=profundo)

async def cmd_estado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    await update.message.reply_text(f"✅ Online\nPendientes: {len(pendientes)}")

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

def main():
    threading.Thread(target=run_http_server, daemon=True).start()
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("scan", cmd_scan))
    app.add_handler(CommandHandler("estado", cmd_estado))
    app.add_handler(CallbackQueryHandler(handle_callback))
    # Escaneo automático cada 15 min
    app.job_queue.run_repeating(monitoreo_wrapper, interval=900, first=30)
    app.run_polling()

if __name__ == "__main__":
    main()
