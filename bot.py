import os, hashlib, requests, logging, threading, asyncio, random
from datetime import datetime
from io import BytesIO
from http.server import HTTPServer, BaseHTTPRequestHandler

import pytz
from bs4 import BeautifulSoup
import google.generativeai as genai
from supabase import create_client, Client

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from telegram.constants import ParseMode

# ─── Configuración ──────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("universo_football")

# Variables de entorno (Asegúrate de que coincidan en Render)
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
        self.wfile.write(b"Universo Football Online")
    def log_message(self, *args): pass

def run_http_server():
    port = int(os.environ.get("PORT", 8080))
    HTTPServer(("0.0.0.0", port), RenderKeepAlive).serve_forever()

# ─── Scraping vía xcancel.com ────────────────────────────────────────────────
def fetch_tweets(user, num=5):
    base_url = "https://xcancel.com"
    logger.info(f"🔎 Buscando en {base_url}/{user}...")
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "es-ES,es;q=0.8,en-US;q=0.5,en;q=0.3",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1"
    }

    try:
        r = requests.get(f"{base_url}/{user}", headers=headers, timeout=20)
        if r.status_code != 200:
            logger.error(f"❌ xcancel devolvió error {r.status_code}")
            return []

        soup = BeautifulSoup(r.text, "html.parser")
        # En xcancel, los tweets suelen estar en .timeline-item o .tweet-body
        items = soup.select(".timeline-item")
        
        if not items:
            logger.warning(f"⚠️ No se detectaron elementos en el timeline de {user}")
            return []

        res = []
        for it in items[:num]:
            # Extraer texto
            txt_elem = it.select_one(".tweet-content")
            if not txt_elem: continue
            texto = txt_elem.get_text(strip=True)
            
            # Extraer link original
            lnk_elem = it.select_one(".tweet-link")
            url_tweet = f"https://x.com{lnk_elem['href']}" if lnk_elem else f"{base_url}/{user}"
            
            # Extraer imagen (si hay)
            img_elem = it.select_one(".attachment img")
            url_img = f"{base_url}{img_elem['src']}" if img_elem else None
            
            res.append({
                "texto": texto,
                "url": url_tweet,
                "img": url_img,
                "user": user
            })
        
        logger.info(f"✅ Se obtuvieron {len(res)} tweets de {user}")
        return res
    except Exception as e:
        logger.error(f"❌ Error crítico en xcancel: {e}")
        return []

# ─── Lógica de Procesamiento ─────────────────────────────────────────────────
async def procesar_tweet(t, context):
    # Generar ID único basado en el contenido para evitar duplicados
    tid = hashlib.md5(t["texto"].encode()).hexdigest()[:12]
    
    # Check en Supabase
    duplicado = supabase.table("noticias").select("id").eq("identificador_ia", tid).execute()
    if duplicado.data: return False

    try:
        # 1. Clasificar con Gemini
        tipo_raw = gemini_model.generate_content(f"Responde solo 'fichaje' o 'noticia': {t['texto'][:150]}")
        tipo = tipo_raw.text.strip().lower()
        
        # 2. Redactar para Universo Football
        prompt = f"Redacta un post de Telegram con emojis para este {tipo}: {t['texto']}. Fuente: @{t['user']}. Formato: Impactante y breve."
        redac = gemini_model.generate_content(prompt).text.strip()
        
        # 3. Guardar en DB
        supabase.table("noticias").insert({
            "identificador_ia": tid,
            "url_origen": t["url"],
            "tipo": tipo,
            "estado": "pendiente",
            "texto_final": redac
        }).execute()
        
        # 4. Descargar imagen si existe
        img_data = None
        if t["img"]:
            try: img_data = requests.get(t["img"], timeout=10).content
            except: pass

        pendientes[tid] = {"texto": redac, "foto": img_data}
        
        # 5. Enviar al Admin para aprobación
        btn = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ PUBLICAR", callback_data=f"p:{tid}"),
            InlineKeyboardButton("🗑 BORRAR", callback_data=f"d:{tid}")
        ]])
        
        if img_data:
            await context.bot.send_photo(ADMIN_ID, BytesIO(img_data), caption=f"🆔 `{tid}`\n\n{redac}"[:1024], parse_mode=ParseMode.MARKDOWN, reply_markup=btn)
        else:
            await context.bot.send_message(ADMIN_ID, f"🆔 `{tid}`\n\n{redac}", parse_mode=ParseMode.MARKDOWN, reply_markup=btn)
        return True
    except Exception as e:
        logger.error(f"Error procesando tweet: {e}")
        return False

# ─── Tareas en segundo plano ─────────────────────────────────────────────────
async def monitoreo_wrapper(context: ContextTypes.DEFAULT_TYPE):
    profundo = context.job.data if context.job and context.job.data else False
    num_tweets = 12 if profundo else 4
    encontrados = 0
    
    logger.info(f"--- Iniciando monitoreo Universo Football ---")
    for cuenta in CUENTAS_X:
        tweets = fetch_tweets(cuenta, num_tweets)
        for tweet in tweets:
            if await procesar_tweet(tweet, context):
                encontrados += 1
            await asyncio.sleep(2) # Respetar rate limits
        await asyncio.sleep(5)
    
    if encontrados == 0:
        await context.bot.send_message(ADMIN_ID, "📭 Escaneo finalizado. No hay nada nuevo en xcancel.")

# ─── Handlers ────────────────────────────────────────────────────────────────
async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    profundo = True if (context.args and context.args[0] == "2h") else False
    await update.message.reply_text(f"🔎 Escaneando xcancel.com ({'2h' if profundo else 'reciente'})...")
    context.job_queue.run_once(monitoreo_wrapper, when=0, data=profundo)

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    accion, tid = q.data.split(":")
    
    if tid in pendientes and accion == "p":
        item = pendientes[tid]
        if item["foto"]:
            await context.bot.send_photo(CHANNEL_ID, BytesIO(item["foto"]), caption=item["texto"][:1024], parse_mode=ParseMode.MARKDOWN)
        else:
            await context.bot.send_message(CHANNEL_ID, item["texto"], parse_mode=ParseMode.MARKDOWN)
        supabase.table("noticias").update({"estado": "publicado"}).eq("identificador_ia", tid).execute()
    
    if tid in pendientes: del pendientes[tid]
    await q.edit_message_reply_markup(None)

# ─── MAIN ───────────────────────────────────────────────────────────────────
def main():
    # Iniciar servidor para Render
    threading.Thread(target=run_http_server, daemon=True).start()
    
    app = Application.builder().token(TOKEN).build()
    
    app.add_handler(CommandHandler("scan", cmd_scan))
    app.add_handler(CallbackQueryHandler(handle_callback))
    
    # Auto-monitoreo cada 15 min
    app.job_queue.run_repeating(monitoreo_wrapper, interval=900, first=10)
    
    logger.info("🚀 Bot Universo Football (xcancel Edition) Iniciado")
    app.run_polling()

if __name__ == "__main__":
    main()
