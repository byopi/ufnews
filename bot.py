import os, hashlib, requests, logging, threading, asyncio
from io import BytesIO
from http.server import HTTPServer, BaseHTTPRequestHandler
import xml.etree.ElementTree as ET # Para leer el RSS

import google.generativeai as genai
from supabase import create_client, Client
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from telegram.constants import ParseMode

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
        self.wfile.write(b"Universo Football RSS Online")
    def log_message(self, *args): pass

def run_http_server():
    port = int(os.environ.get("PORT", 8080))
    HTTPServer(("0.0.0.0", port), RenderKeepAlive).serve_forever()

# ─── Obtención vía RSS (Gratis y Estable) ────────────────────────────────────
def fetch_tweets_rss(user, num=5):
    # Usamos una instancia pública de RSS-Bridge configurada para Twitter
    # Nota: Si una instancia falla, puedes buscar otra en 'rss-bridge.org'
    base_url = "https://rssbridge.org/bridge01/" 
    params = {
        "action": "display",
        "bridge": "TwitterBridge",
        "context": "By username",
        "u": user,
        "format": "Atom"
    }
    
    logger.info(f"📡 Solicitando RSS para @{user}...")
    
    try:
        r = requests.get(base_url, params=params, timeout=20)
        if r.status_code != 200:
            logger.error(f"❌ RSS-Bridge falló: {r.status_code}")
            return []

        # Parseamos el XML (Atom format)
        root = ET.fromstring(r.content)
        # El namespace de Atom es necesario para encontrar los tags
        ns = {'atom': 'http://www.w3.org/2005/Atom'}
        
        res = []
        # Buscamos las entradas (tweets)
        for entry in root.findall('atom:entry', ns)[:num]:
            title = entry.find('atom:title', ns).text
            link = entry.find('atom:link', ns).attrib['href']
            content = entry.find('atom:content', ns).text # Aquí suele venir el texto y la imagen
            
            # Limpieza básica de HTML en el contenido si es necesario
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(content, "html.parser")
            texto_limpio = soup.get_text(strip=True)
            
            # Intentar extraer imagen del contenido HTML del RSS
            img_tag = soup.find('img')
            url_img = img_tag['src'] if img_tag else None

            res.append({
                "texto": texto_limpio,
                "url": link,
                "img": url_img,
                "user": user
            })
            
        logger.info(f"✅ RSS: {len(res)} noticias de {user}")
        return res
    except Exception as e:
        logger.error(f"❌ Error en RSS: {e}")
        return []

# ─── Procesamiento e IA (Igual que antes) ───────────────────────────────────
async def procesar_noticia(n, context):
    tid = hashlib.md5(n["texto"].encode()).hexdigest()[:12]
    
    # Duplicados
    if supabase.table("noticias").select("id").eq("identificador_ia", tid).execute().data:
        return False

    try:
        # Clasificación
        tipo = gemini_model.generate_content(f"Dime 'fichaje' o 'noticia': {n['texto'][:100]}").text.strip().lower()
        # Redacción
        prompt = f"Como analista deportivo de 'Universo Football', redacta para Telegram este {tipo}: {n['texto']}. Fuente: @{n['user']}. Usa emojis."
        redac = gemini_model.generate_content(prompt).text.strip()
        
        supabase.table("noticias").insert({
            "identificador_ia": tid, "url_origen": n["url"], 
            "tipo": tipo, "estado": "pendiente", "texto_final": redac
        }).execute()
        
        img_b = requests.get(n["img"]).content if n["img"] else None
        pendientes[tid] = {"texto": redac, "foto": img_b}
        
        btn = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ PUBLICAR", callback_data=f"p:{tid}"), 
            InlineKeyboardButton("🗑 BORRAR", callback_data=f"d:{tid}")
        ]])
        
        cap = f"🆔 `{tid}`\n\n{redac}"
        if img_b: await context.bot.send_photo(ADMIN_ID, BytesIO(img_b), caption=cap[:1024], parse_mode=ParseMode.MARKDOWN, reply_markup=btn)
        else: await context.bot.send_message(ADMIN_ID, cap, parse_mode=ParseMode.MARKDOWN, reply_markup=btn)
        return True
    except: return False

# ─── Monitoreo y Handlers (Ajustados) ────────────────────────────────────────
async def monitoreo_wrapper(context: ContextTypes.DEFAULT_TYPE):
    profundo = context.job.data if context.job and context.job.data else False
    num = 10 if profundo else 3
    encontrados = 0
    
    for c in CUENTAS_X:
        items = fetch_tweets_rss(c, num)
        for item in items:
            if await procesar_noticia(item, context): encontrados += 1
            await asyncio.sleep(1)
        await asyncio.sleep(2)
    
    if encontrados == 0:
        await context.bot.send_message(ADMIN_ID, "📭 Sin novedades vía RSS.")

async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    await update.message.reply_text("🔎 Escaneando feeds RSS...")
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
