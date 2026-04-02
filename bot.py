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
import feedparser

# ─── Configuración ──────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("universo_football")

TOKEN          = os.environ.get("TELEGRAM_BOT_TOKEN")
ADMIN_ID       = int(os.environ.get("ADMIN_TELEGRAM_ID", 0))
CHANNEL_ID     = os.environ.get("TELEGRAM_CHANNEL_ID")
SUPABASE_URL   = os.environ.get("SUPABASE_URL")
SUPABASE_KEY   = os.environ.get("SUPABASE_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# --- CONFIGURACIÓN GEMINI (FORZADO PARA EVITAR 404) ---
try:
    # Forzamos el transporte 'rest' para evitar conflictos de v1beta en Render
    genai.configure(api_key=GEMINI_API_KEY, transport='rest')
    
    gemini_model = genai.GenerativeModel(
        model_name="gemini-1.5-flash"
    )
    logger.info("✅ Configuración de Gemini cargada correctamente")
except Exception as e:
    logger.error(f"❌ Error al configurar Gemini: {e}")

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

# ─── Obtención de RSS (Solo fuentes estables) ────────────────────────────────
def fetch_tweets_rss(user, num=5):
    instancias = [
        f"https://nitter.net/{user}/rss",
        f"https://xcancel.com/{user}/rss"
    ]
    
    for url in instancias:
        logger.info(f"📡 Intentando @{user} en: {url}")
        try:
            feed = feedparser.parse(url, agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) NewsReader/1.0')
            
            if len(feed.entries) > 0:
                res = []
                for entry in feed.entries[:num]:
                    content = entry.get('description', entry.get('summary', ''))
                    soup = BeautifulSoup(content, "html.parser")
                    texto_limpio = soup.get_text(strip=True)
                    if not texto_limpio: texto_limpio = entry.get('title', '')

                    img_tag = soup.find('img')
                    url_img = img_tag['src'] if img_tag else None

                    res.append({
                        "texto": texto_limpio,
                        "url": entry.link,
                        "img": url_img,
                        "user": user
                    })
                logger.info(f"✅ ¡LOGRADO! {len(res)} noticias de {user}")
                return res
        except Exception as e:
            continue
    return []

# ─── Comandos ───────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    msg = (
        "*🟢 Bot iniciado asere*\n\n"
        "/estado — Estado del bot\n"
        "/pendientes — Noticias en espera\n"
        "/scan — Forzar escaneo ahora\n"
        "/scan 'N°h' — Fuerza el escaneo desde publicaciones de horas anteriores"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

async def cmd_estado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    count = supabase.table("noticias").select("id", count="exact").execute().count
    await update.message.reply_text(f"✅ *En línea*\n📊 Total en DB: *{count}*")

async def cmd_pendientes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    if not pendientes:
        await update.message.reply_text("📭 No hay noticias pendientes.")
        return
    await update.message.reply_text(f"📝 Tienes *{len(pendientes)}* noticias esperando.")

# ─── Lógica de Procesamiento ──────────────────────────────────────────────────
async def procesar_noticia(n, context):
    tid = hashlib.md5(n["texto"].encode()).hexdigest()[:12]
    
    # 1. Duplicados
    try:
        existe = supabase.table("noticias").select("id").eq("identificador_ia", tid).execute()
        if existe.data: return False
    except: return False

    # 2. IA (Redacción con estilo)
    try:
        logger.info(f"🤖 Redactando noticia {tid}...")
        prompt = (
            f"Eres el redactor estrella de 'Universo Football'. Estilo directo y emocionante. "
            f"Redacta un post para Telegram: {n['texto']}. Fuente: @{n['user']}. "
            f"Usa negritas para equipos y jugadores. Termina con un hashtag futbolero."
        )
        res_ia = gemini_model.generate_content(prompt)
        redac = res_ia.text.strip()
    except Exception as e:
        logger.error(f"❌ Error Gemini: {e}")
        return False

    # 3. Guardar e informar al Admin
    try:
        supabase.table("noticias").insert({
            "identificador_ia": tid, "url_origen": n["url"], 
            "estado": "pendiente", "texto_final": redac
        }).execute()

        img_b = None
        if n["img"]:
            try: img_b = requests.get(n["img"]).content
            except: pass

        pendientes[tid] = {"texto": redac, "foto": img_b}
        btn = InlineKeyboardMarkup([[InlineKeyboardButton("✅ PUBLICAR", callback_data=f"p:{tid}"), InlineKeyboardButton("🗑 BORRAR", callback_data=f"d:{tid}")]])
        
        cap = f"🆔 `{tid}`\n\n{redac}"
        if img_b: await context.bot.send_photo(ADMIN_ID, BytesIO(img_b), caption=cap[:1024], parse_mode=ParseMode.MARKDOWN, reply_markup=btn)
        else: await context.bot.send_message(ADMIN_ID, cap, parse_mode=ParseMode.MARKDOWN, reply_markup=btn)
        return True
    except: return False

# ─── Monitoreo ──────────────────────────────────────────────────────────────
async def monitoreo_wrapper(context: ContextTypes.DEFAULT_TYPE):
    logger.info("--- Iniciando Monitoreo ---")
    encontrados = 0
    totales = 0
    for c in CUENTAS_X:
        items = fetch_tweets_rss(c)
        totales += len(items)
        for item in items:
            if await procesar_noticia(item, context): encontrados += 1
            await asyncio.sleep(1)

    if encontrados == 0:
        texto = (
            "📭 *Escaneo finalizado*\n"
            f"Revisados: *{totales} posts.*\n"
            "Nuevos para aprobar: *0.*\n\n"
            "_Si esto sigue en 0, es probable que las fuentes estén saturadas._"
        )
        await context.bot.send_message(ADMIN_ID, texto, parse_mode=ParseMode.MARKDOWN)

async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    await update.message.reply_text("🔎 *Escaneando ahora mismo...*", parse_mode=ParseMode.MARKDOWN)
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
