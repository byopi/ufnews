import os, hashlib, requests, logging, threading, asyncio, random  # <--- IMPORT AGREGADO
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
        self.wfile.write(b"Universo Football xcancel RSS Online")
    def log_message(self, *args): pass

def run_http_server():
    port = int(os.environ.get("PORT", 8080))
    HTTPServer(("0.0.0.0", port), RenderKeepAlive).serve_forever()

# ─── Obtención vía xcancel RSS ───────────────────────────────────────────────
def fetch_tweets_rss(user, num=5):
    # Usamos xcancel directamente como feed RSS
    url = f"https://xcancel.com/{user}/rss"
    
    logger.info(f"📡 Solicitando RSS nativo de xcancel para @{user}...")
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
    }

    try:
        r = requests.get(url, headers=headers, timeout=20)
        if r.status_code != 200:
            logger.error(f"❌ xcancel RSS falló para {user}: {r.status_code}")
            return []

        # El RSS de xcancel suele ser formato RSS 2.0 (usa <item> en lugar de <entry>)
        root = ET.fromstring(r.content)
        channel = root.find('channel')
        
        res = []
        if channel is not None:
            for item in channel.findall('item')[:num]:
                title = item.find('title').text if item.find('title') is not None else ""
                link = item.find('link').text if item.find('link') is not None else ""
                description = item.find('description').text if item.find('description') is not None else ""
                
                # Limpiamos el HTML de la descripción (donde viene el tweet y a veces imágenes)
                soup = BeautifulSoup(description, "html.parser")
                texto_limpio = soup.get_text(strip=True)
                
                # Intentar buscar imagen en la descripción
                img_tag = soup.find('img')
                url_img = img_tag['src'] if img_tag else None

                res.append({
                    "texto": texto_limpio or title,
                    "url": link,
                    "img": url_img,
                    "user": user
                })
            
        logger.info(f"✅ xcancel RSS: {len(res)} noticias de {user}")
        return res
    except Exception as e:
        logger.error(f"❌ Error procesando xcancel RSS para {user}: {e}")
        return []

# ─── Procesamiento con IA ───────────────────────────────────────────────────
async def procesar_noticia(n, context):
    tid = hashlib.md5(n["texto"].encode()).hexdigest()[:12]
    
    if supabase.table("noticias").select("id").eq("identificador_ia", tid).execute().data:
        return False

    try:
        # Clasificación con Gemini
        tipo_res = gemini_model.generate_content(f"Dime 'fichaje' o 'noticia': {n['texto'][:100]}")
        tipo = tipo_res.text.strip().lower()
        
        # Redacción estilo Universo Football
        prompt = f"Como analista de 'Universo Football', redacta este {tipo} para Telegram: {n['texto']}. Fuente: @{n['user']}. Usa emojis futboleros."
        redac = gemini_model.generate_content(prompt).text.strip()
        
        supabase.table("noticias").insert({
            "identificador_ia": tid, "url_origen": n["url"], 
            "tipo": tipo, "estado": "pendiente", "texto_final": redac
        }).execute()
        
        img_b = None
        if n["img"]:
            try: img_b = requests.get(n["img"], timeout=10).content
            except: pass

        pendientes[tid] = {"texto": redac, "foto": img_b}
        
        btn = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ PUBLICAR", callback_data=f"p:{tid}"), 
            InlineKeyboardButton("🗑 BORRAR", callback_data=f"d:{tid}")
        ]])
        
        cap = f"🆔 `{tid}`\n\n{redac}"
        if img_b:
            await context.bot.send_photo(ADMIN_ID, BytesIO(img_b), caption=cap[:1024], parse_mode=ParseMode.MARKDOWN, reply_markup=btn)
        else:
            await context.bot.send_message(ADMIN_ID, cap, parse_mode=ParseMode.MARKDOWN, reply_markup=btn)
        return True
    except Exception as e:
        logger.error(f"Error en IA: {e}")
        return False

# ─── Monitoreo ───────────────────────────────────────────────────────────────
async def monitoreo_wrapper(context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"--- Iniciando monitoreo Universo Football (xcancel RSS) ---")
    encontrados = 0
    for c in CUENTAS_X:
        items = fetch_tweets_rss(c, num=5)
        for item in items:
            if await procesar_noticia(item, context): 
                encontrados += 1
            await asyncio.sleep(1)
    
    if encontrados == 0:
        logger.info("Nada nuevo encontrado.")

# ─── Comandos y Main ─────────────────────────────────────────────────────────
async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    await update.message.reply_text("🔎 Escaneando xcancel RSS...")
    context.job_queue.run_once(monitoreo_wrapper, when=0)

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
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.job_queue.run_repeating(monitoreo_wrapper, interval=900, first=10)
    app.run_polling()

if __name__ == "__main__": 
    main()
