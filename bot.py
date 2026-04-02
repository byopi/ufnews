import os, hashlib, requests, logging, threading, asyncio, random
import xml.etree.ElementTree as ET
from io import BytesIO
from http.server import HTTPServer, BaseHTTPRequestHandler

import google.generativeai as genai
from supabase import create_client, Client
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from telegram.constants import ParseMode
from bs4 import BeautifulSoup

# ─── Configuración ──────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("universo_football")

TOKEN          = os.environ.get("TELEGRAM_BOT_TOKEN")
ADMIN_ID       = int(os.environ.get("ADMIN_TELEGRAM_ID", 0))
CHANNEL_ID     = os.environ.get("TELEGRAM_CHANNEL_ID")
SUPABASE_URL   = os.environ.get("SUPABASE_URL")
SUPABASE_KEY   = os.environ.get("SUPABASE_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

genai.configure(api_key=GEMINI_API_KEY)
gemini_model = genai.GenerativeModel("gemini-1.5-flash")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

CUENTAS_X = ["mercatosphera", "Mercado_Ingles", "SoyCalcio_", "postunited"]
pendientes = {}

# ─── Servidor Keep-Alive ─────────────────────────────────────────────────────
class RenderKeepAlive(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers()
        self.wfile.write(b"Universo Football Bot Active")
    def log_message(self, *args): pass

def run_http_server():
    port = int(os.environ.get("PORT", 8080))
    HTTPServer(("0.0.0.0", port), RenderKeepAlive).serve_forever()

import feedparser
import random

def fetch_tweets_rss(user, num=5):
    # Lista de espejos diferentes. Si uno da 400, el otro lo sacará.
    instancias = [
        f"https://nitter.privacydev.net/{user}/rss",
        f"https://nitter.no-logs.com/{user}/rss",
        f"https://nitter.perennialte.ch/{user}/rss",
        f"https://xcancel.com/{user}/rss"
    ]
    
    # Desordenamos para no saturar siempre la misma
    random.shuffle(instancias)
    
    for url in instancias:
        logger.info(f"📡 Probando @{user} en: {url}")
        try:
            # feedparser es más robusto que requests para esto
            feed = feedparser.parse(url, agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) NewsReader/1.0')
            
            # Verificamos si el feed tiene entradas
            if len(feed.entries) > 0:
                res = []
                for entry in feed.entries[:num]:
                    content = entry.get('description', entry.get('summary', ''))
                    soup = BeautifulSoup(content, "html.parser")
                    
                    # Limpieza de texto
                    texto_limpio = soup.get_text(strip=True)
                    if not texto_limpio: texto_limpio = entry.get('title', '')

                    # Buscar imagen
                    img_tag = soup.find('img')
                    url_img = img_tag['src'] if img_tag else None

                    res.append({
                        "texto": texto_limpio,
                        "url": entry.link,
                        "img": url_img,
                        "user": user
                    })
                
                logger.info(f"✅ ¡LOGRADO! {len(res)} noticias de {user} desde {url}")
                return res
            else:
                logger.warning(f"⚠️ {url} respondió pero el feed está vacío.")
                
        except Exception as e:
            logger.error(f"❌ Falló {url}: {e}")
            continue

    logger.error(f"💀 Todas las fuentes fallaron para @{user}")
    return []
        
# ─── Comandos Solicitados ────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    msg = (
        "**🟢 Bot iniciado asere**\n\n"
        "/estado — Estado del bot\n"
        "/pendientes — Noticias en espera\n"
        "/scan — Forzar escaneo ahora\n"
        "/scan 'N°h' — Fuerza el escaneo desde publicaciones de horas anteriores"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

async def cmd_estado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    count = supabase.table("noticias").select("id", count="exact").execute().count
    await update.message.reply_text(f"✅ **En línea**\n📊 Total en DB: {count}\n🕒 RSS: xcancel.com")

async def cmd_pendientes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    if not pendientes:
        await update.message.reply_text("📭 No hay noticias pendientes de aprobación.")
        return
    await update.message.reply_text(f"📝 Tienes {len(pendientes)} noticias esperando en el chat.")

# ─── Lógica de Procesamiento ──────────────────────────────────────────────────
async def procesar_noticia(n, context):
    tid = hashlib.md5(n["texto"].encode()).hexdigest()[:12]
    if supabase.table("noticias").select("id").eq("identificador_ia", tid).execute().data:
        return False
    try:
        res_ia = gemini_model.generate_content(f"Redacta un post breve para Telegram: {n['texto']}. Fuente: @{n['user']}")
        redac = res_ia.text.strip()
        supabase.table("noticias").insert({"identificador_ia": tid, "url_origen": n["url"], "estado": "pendiente", "texto_final": redac}).execute()
        
        img_b = requests.get(n["img"]).content if n["img"] else None
        pendientes[tid] = {"texto": redac, "foto": img_b}
        
        btn = InlineKeyboardMarkup([[InlineKeyboardButton("✅ PUBLICAR", callback_data=f"p:{tid}"), InlineKeyboardButton("🗑 BORRAR", callback_data=f"d:{tid}")]])
        if img_b: await context.bot.send_photo(ADMIN_ID, BytesIO(img_b), caption=f"🆔 `{tid}`\n\n{redac}", reply_markup=btn)
        else: await context.bot.send_message(ADMIN_ID, f"🆔 `{tid}`\n\n{redac}", reply_markup=btn)
        return True
    except: return False

# ─── Monitoreo con aviso de "Vacío" ──────────────────────────────────────────
async def monitoreo_wrapper(context: ContextTypes.DEFAULT_TYPE):
    logger.info("--- Iniciando Monitoreo Universo Football ---")
    encontrados_nuevos = 0
    totales_revisados = 0

    for c in CUENTAS_X:
        items = fetch_tweets_rss(c)
        totales_revisados += len(items)
        for item in items:
            if await procesar_noticia(item, context):
                encontrados_nuevos += 1
            await asyncio.sleep(1)

    # Si terminó y no hay nada, avisa al admin para que no te quedes esperando
    if encontrados_nuevos == 0:
        await context.bot.send_message(
            ADMIN_ID, 
            f"📭 **Escaneo finalizado**\nRevisados: {totales_revisados} posts.\nNuevos para aprobar: 0.\n\n_Si esto sigue en 0, es probable que xcancel esté bloqueando la IP de Render._"
        )

async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    h = context.args[0] if context.args else "recientes"
    await update.message.reply_text(f"🔎 Escaneando posts ({h})...")
    context.job_queue.run_once(monitoreo_wrapper, when=0)

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    act, tid = q.data.split(":")
    if tid in pendientes and act == "p":
        d = pendientes[tid]
        if d["foto"]: await context.bot.send_photo(CHANNEL_ID, BytesIO(d["foto"]), caption=d["texto"])
        else: await context.bot.send_message(CHANNEL_ID, d["texto"])
        supabase.table("noticias").update({"estado": "publicado"}).eq("identificador_ia", tid).execute()
    if tid in pendientes: del pendientes[tid]
    await q.edit_message_reply_markup(None)

# ─── Main ────────────────────────────────────────────────────────────────────
def main():
    threading.Thread(target=run_http_server, daemon=True).start()
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("estado", cmd_estado))
    app.add_handler(CommandHandler("pendientes", cmd_pendientes))
    app.add_handler(CommandHandler("scan", cmd_scan))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.job_queue.run_repeating(monitoreo_wrapper, interval=900, first=10)
    app.run_polling()

if __name__ == "__main__": 
    main()
