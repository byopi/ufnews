import os, hashlib, requests, logging, threading, asyncio, random
import xml.etree.ElementTree as ET
from io import BytesIO
from http.server import HTTPServer, BaseHTTPRequestHandler

from groq import Groq
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
GROQ_API_KEY   = os.environ.get("GROQ_API_KEY")

# Clientes
client_groq = Groq(api_key=GROQ_API_KEY)
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

# ─── Obtención de RSS ────────────────────────────────────────────────────────
def fetch_tweets_rss(user, num=5):
    instancias = [
        f"https://nitter.net/{user}/rss",
        f"https://xcancel.com/{user}/rss",
        f"https://nitter.cz/{user}/rss",
        f"https://nitter.privacydev.net/{user}/rss",
        f"https://nitter.no-logs.com/{user}/rss"
    ]
    random.shuffle(instancias)
    palabras_basura = ["whitelist", "ignore", "rss reader", "send an email", "plain request"]

    for url in instancias:
        try:
            feed = feedparser.parse(url, agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) NewsReader/1.0')
            if len(feed.entries) > 0:
                res = []
                for entry in feed.entries[:num]:
                    content = entry.get('description', entry.get('summary', ''))
                    soup = BeautifulSoup(content, "html.parser")
                    texto_limpio = soup.get_text(strip=True)
                    if not texto_limpio: texto_limpio = entry.get('title', '')
                    if any(bad in texto_limpio.lower() for bad in palabras_basura): continue
                    img_tag = soup.find('img')
                    url_img = img_tag['src'] if img_tag else None
                    res.append({"texto": texto_limpio, "url": entry.link, "img": url_img, "user": user})
                if res: return res
        except: continue
    return []

# ─── Comandos ───────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    msg = "*🟢 Bot iniciado asere*\n\n/estado — Estado\n/pendientes — En espera\n/scan — Forzar ahora"
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

async def cmd_estado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    count = supabase.table("noticias").select("id", count="exact").execute().count
    await update.message.reply_text(f"✅ *En línea*\n📊 Total en DB: *{count}*")

async def cmd_pendientes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    await update.message.reply_text(f"📝 Tienes *{len(pendientes)}* noticias esperando.")

# ─── Lógica de Procesamiento ──────────────────────────────────────────────────
async def procesar_noticia(n, context):
    tid = hashlib.md5(n["texto"].encode()).hexdigest()[:12]
    try:
        existe = supabase.table("noticias").select("id").eq("identificador_ia", tid).execute()
        if existe.data: return False
    except: return False

    # 2. IA con GROQ (Modelo Actualizado y Formato Visual Pro)
    try:
        logger.info(f"🤖 Redactando noticia {tid} con Groq...")
        completion = client_groq.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": (
                    "Eres el redactor jefe de 'Universo Football'. Tu misión es transformar noticias en posts de Telegram visualmente atractivos.\n\n"
                    "ESTRUCTURA OBLIGATORIA DEL POST:\n"
                    "1. Titular impactante en NEGRITA con emojis relacionados (Ej: ✅🇳🇴 **FICHAJE CONFIRMADO: Stian Molde**).\n"
                    "2. DOBLE SALTO DE LÍNEA (espacio en blanco).\n"
                    "3. Cuerpo de la noticia dividido en párrafos CORTOS y SEPARADOS por doble salto de línea.\n"
                    "4. Cada párrafo del cuerpo DEBE empezar con un emoji de viñeta (Ej: ➡️, ℹ️, ↪️, ◽️, ◼️).\n"
                    "5. Usa negritas (**) para nombres de EQUIPOS, JUGADORES y COMPETICIONES.\n"
                    "6. DOBLE SALTO DE LÍNEA antes de la firma.\n"
                    "7. Firma obligatoria en NEGRITA: 📲 **Suscríbete en t.me/iUniversoFootball**\n\n"
                    "REGLAS:\n"
                    "- PARAFRASEA, no copies. Varía los titulares.\n"
                    "- Limpia los hashtags (#) del texto original.\n"
                    "- NO digas 'Aquí tienes el post'."
                )},
                {"role": "user", "content": f"Parafrasea esta noticia de @{n['user']} con párrafos separados y emojis de viñeta: {n['texto']}"}
            ],
            temperature=0.6,
            max_tokens=800
        )
        redac = completion.choices[0].message.content.strip()
        logger.info(f"✅ Redacción de Groq lista.")
    except Exception as e:
        logger.error(f"❌ Groq falló: {e}. Usando fallback.")
        texto_f = n['texto'].replace("#", "")
        redac = f"📢 **NOTICIA** (@{n['user']})\n\n{texto_f}\n\n📲 **Suscríbete en t.me/iUniversoFootball**"

    # 3. Guardar en Supabase
    try:
        supabase.table("noticias").insert({
            "identificador_ia": tid, "url_origen": n["url"], 
            "estado": "pendiente", "texto_final": redac
        }).execute()
    except Exception as e:
        logger.error(f"❌ Error Supabase: {e}")
        return False
    
    # 4. Enviar al Admin
    try:
        img_b = None
        if n["img"]:
            try:
                r_img = requests.get(n["img"], timeout=10)
                if r_img.status_code == 200: img_b = r_img.content
            except: pass

        pendientes[tid] = {"texto": redac, "foto": img_b}
        btn = InlineKeyboardMarkup([[InlineKeyboardButton("✅ PUBLICAR", callback_data=f"p:{tid}"), InlineKeyboardButton("🗑 BORRAR", callback_data=f"d:{tid}")]])
        cap = f"🆔 `{tid}`\n\n{redac}"
        if img_b:
            await context.bot.send_photo(ADMIN_ID, BytesIO(img_b), caption=cap[:1024], parse_mode=ParseMode.MARKDOWN, reply_markup=btn)
        else:
            await context.bot.send_message(ADMIN_ID, cap, parse_mode=ParseMode.MARKDOWN, reply_markup=btn)
        return True
    except Exception as e:
        logger.error(f"❌ Error al enviar al Admin: {e}")
        return False

# ─── Monitoreo ──────────────────────────────────────────────────────────────
async def monitoreo_wrapper(context: ContextTypes.DEFAULT_TYPE):
    logger.info("--- Iniciando Monitoreo Universo Football ---")
    encontrados = 0
    for c in CUENTAS_X:
        items = fetch_tweets_rss(c)
        for item in items:
            if await procesar_noticia(item, context): encontrados += 1
            await asyncio.sleep(2)
    if encontrados == 0:
        logger.info("Escaneo finalizado sin novedades.")

async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    await update.message.reply_text("🔎 *Escaneando...*", parse_mode=ParseMode.MARKDOWN)
    context.job_queue.run_once(monitoreo_wrapper, when=0)

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    act, tid = q.data.split(":")
    if tid in pendientes and act == "p":
        d = pendientes[tid]
        try:
            if d["foto"]: await context.bot.send_photo(CHANNEL_ID, BytesIO(d["foto"]), caption=d["texto"], parse_mode=ParseMode.MARKDOWN)
            else: await context.bot.send_message(CHANNEL_ID, d["texto"], parse_mode=ParseMode.MARKDOWN)
            supabase.table("noticias").update({"estado": "publicado"}).eq("identificador_ia", tid).execute()
        except Exception as e:
            logger.error(f"❌ Error al publicar: {e}")
    if tid in pendientes: del pendientes[tid]
    await q.edit_message_reply_markup(None)

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
