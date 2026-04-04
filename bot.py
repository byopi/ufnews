import os, hashlib, requests, logging, threading, asyncio, random, re
from io import BytesIO
from datetime import datetime, timedelta
import pytz
from http.server import HTTPServer, BaseHTTPRequestHandler

from groq import Groq
from supabase import create_client, Client
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from telegram.constants import ParseMode
from bs4 import BeautifulSoup
import feedparser

# ─── Configuración ──────────────────────────────────────────────────────────
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger("universo_football")

TOKEN          = os.environ.get("TELEGRAM_BOT_TOKEN")
ADMIN_ID       = int(os.environ.get("ADMIN_TELEGRAM_ID", 0))
CHANNEL_ID     = os.environ.get("TELEGRAM_CHANNEL_ID")
SUPABASE_URL   = os.environ.get("SUPABASE_URL")
SUPABASE_KEY   = os.environ.get("SUPABASE_KEY")
GROQ_API_KEY   = os.environ.get("GROQ_API_KEY")

client_groq = Groq(api_key=GROQ_API_KEY)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
VENEZUELA_TZ = pytz.timezone('America/Caracas')

CUENTAS_X = ["mercatosphera", "Mercado_Ingles", "SoyCalcio_", "postunited", "laligaa_neews"]
pendientes = {}
esperando_foto = {}
esperando_hora = {}
esperando_edicion = {}

# ─── Servidor Keep-Alive (Para Render/UptimeRobot) ──────────────────────────
class RenderKeepAlive(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers()
        self.wfile.write(b"Universo Football Bot Active")
    def log_message(self, *args): pass

def run_http_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), RenderKeepAlive)
    server.serve_forever()

# ─── Obtención de RSS ────────────────────────────────────────────────────────
def fetch_tweets_rss(user, num=5):
    instancias = [f"https://nitter.net/{user}/rss", f"https://xcancel.com/{user}/rss", f"https://nitter.cz/{user}/rss"]
    random.shuffle(instancias)
    for url in instancias:
        try:
            feed = feedparser.parse(url, agent='Mozilla/5.0')
            if len(feed.entries) > 0:
                res = []
                for entry in feed.entries[:num]:
                    soup = BeautifulSoup(entry.get('description', ''), "html.parser")
                    texto = soup.get_text(strip=True)
                    if not texto or "rss reader" in texto.lower(): continue
                    img = soup.find('img')['src'] if soup.find('img') else None
                    res.append({"texto": texto, "url": entry.link, "img": img, "user": user})
                if res: return res
        except: continue
    return []

# ─── Lógica de Procesamiento ────────────────────────────────────────────────
async def procesar_noticia(n, context):
    # Hash robusto: Texto + URL para evitar colisiones en noticias similares
    semilla = f"{n['texto']}{n['url']}".encode()
    tid = hashlib.md5(semilla).hexdigest()[:12]
    
    try:
        # 1. ¿Ya existe en Supabase?
        res = supabase.table("noticias").select("estado").eq("identificador_ia", tid).execute()
        if res.data and len(res.data) > 0:
            return False
            
    except Exception as e:
        logger.error(f"Error consultando Supabase: {e}")
        return False

    # 2. PROCESAR CON LA IA
    try:
        completion = client_groq.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": (
                    "Eres el redactor de 'Universo Football'.\n\n"
                    "ESTRUCTURA HTML:\n"
                    "🚨🌍 | <b>Titular</b>\n\n"
                    "▫️ Hecho 1 (máx 2 líneas).\n"
                    "▫️ Hecho 2 (máx 2 líneas).\n\n"
                    "<b>ℹ️ » [Nombre]</b> (SOLO si hay fuente clara)\n\n"
                    "📲 <b>Suscríbete en t.me/iUniversoFootball</b>\n\n"
                    "REGLAS:\n"
                    "- Usa ÚNICAMENTE el emoji ▫️ para los hechos.\n"
                    "- Prohibido usar el espacio invisible de Telegram (\\xa0).\n"
                    "- Mantén la temperatura en 0.1."
                )},
                {"role": "user", "content": f"Redacta esta noticia limpia: {n['texto']}"}
            ],
            temperature=0.1
        )
        redac = completion.choices[0].message.content.strip().replace('\xa0', '').replace('  ', ' ')
    except Exception as e:
        logger.error(f"Error Groq: {e}")
        return False 

    # 3. PREPARAR IMAGEN
    img_b = None
    if n["img"]:
        try:
            r = requests.get(n["img"], timeout=10)
            if r.status_code == 200: img_b = r.content
        except: pass

    # 4. REGISTRO FINAL Y ENVÍO
    try:
        supabase.table("noticias").insert({
            "identificador_ia": tid, 
            "url_origen": n["url"], 
            "estado": "en_revision"
        }).execute()
        
        pendientes[tid] = {"texto": redac, "foto": img_b, "url": n["url"]}
        await enviar_panel_control(tid, context)
        logger.info(f"✅ Noticia enviada al panel: {tid}")
        return True
        
    except Exception as e:
        logger.error(f"Error al registrar éxito en Supabase: {e}")
        return False

async def enviar_panel_control(tid, context):
    d = pendientes[tid]
    btn = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ PUBLICAR", callback_data=f"p:{tid}"), InlineKeyboardButton("⏰ PROGRAMAR", callback_data=f"s:{tid}")],
        [InlineKeyboardButton("📝 EDITAR TEXTO", callback_data=f"e:{tid}"), InlineKeyboardButton("🖼 CAMBIAR IMG", callback_data=f"f:{tid}")],
        [InlineKeyboardButton("🗑 BORRAR", callback_data=f"d:{tid}")]
    ])
    cap = f"🆔 <code>{tid}</code>\n\n{d['texto']}"
    if d["foto"]:
        await context.bot.send_photo(ADMIN_ID, BytesIO(d["foto"]), caption=cap, parse_mode=ParseMode.HTML, reply_markup=btn)
    else:
        await context.bot.send_message(ADMIN_ID, cap, parse_mode=ParseMode.HTML, reply_markup=btn)

# ─── Comandos ───────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and update.effective_user.id == ADMIN_ID:
        await update.message.reply_text("👋 <b>Universo Football Bot</b>\n\n/scan - Forzar búsqueda\n/estado - Info bot", parse_mode=ParseMode.HTML)

async def cmd_estado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and update.effective_user.id == ADMIN_ID:
        ahora_ccs = datetime.now(VENEZUELA_TZ).strftime("%H:%M:%S")
        await update.message.reply_text(f"✅ <b>Online</b>\n📍 Hora CCS: {ahora_ccs}\n📦 Pendientes: {len(pendientes)}", parse_mode=ParseMode.HTML)

async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and update.effective_user.id == ADMIN_ID:
        await update.message.reply_text("🔎 Escaneando fuentes...")
        await monitoreo_wrapper(context)

# ─── Callbacks & Input ─────────────────────────────────────────────────────
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not q.from_user or q.from_user.id != ADMIN_ID: return
    await q.answer()
    act, tid = q.data.split(":")
    if tid not in pendientes: return

    if act == "p": await publicar_ahora(tid, context)
    elif act == "s":
        esperando_hora[ADMIN_ID] = tid
        await context.bot.send_message(ADMIN_ID, "⏰ Hora Caracas (24h, ej: 15:30):")
    elif act == "e":
        esperando_edicion[ADMIN_ID] = tid
        await context.bot.send_message(ADMIN_ID, "📝 Envía el nuevo texto:")
    elif act == "f":
        esperando_foto[ADMIN_ID] = tid
        await context.bot.send_message(ADMIN_ID, "📸 Envía la nueva foto:")
    elif act == "d":
        if tid in pendientes: del pendientes[tid]
        await q.delete_message()

async def recibir_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or update.effective_user.id != ADMIN_ID: return
    uid = update.effective_user.id

    if uid in esperando_hora:
        tid = esperando_hora.pop(uid)
        try:
            h, m = map(int, update.message.text.split(":"))
            ahora = datetime.now(VENEZUELA_TZ)
            prog = ahora.replace(hour=h, minute=m, second=0, microsecond=0)
            if prog < ahora: prog += timedelta(days=1)
            context.job_queue.run_once(lambda ctx: publicar_ahora(tid, ctx), when=prog.astimezone(pytz.UTC), name=tid)
            await update.message.reply_text(f"✅ Programado para las {update.message.text} (CCS)")
        except: await update.message.reply_text("❌ Formato inválido. Usa HH:MM")

    elif uid in esperando_edicion:
        tid = esperando_edicion.pop(uid)
        pendientes[tid]["texto"] = update.message.text_html.replace('\xa0', '').strip()
        await enviar_panel_control(tid, context)

    elif uid in esperando_foto:
        tid = esperando_foto.pop(uid)
        if update.message.photo:
            foto = await update.message.photo[-1].get_file()
            pendientes[tid]["foto"] = await foto.download_as_bytearray()
            await enviar_panel_control(tid, context)

async def publicar_ahora(tid, context):
    d = pendientes.get(tid)
    if not d: return
    try:
        if d["foto"]:
            await context.bot.send_photo(CHANNEL_ID, BytesIO(d["foto"]), caption=d["texto"], parse_mode=ParseMode.HTML)
        else:
            await context.bot.send_message(CHANNEL_ID, d["texto"], parse_mode=ParseMode.HTML)
        
        supabase.table("noticias").update({"estado": "publicado"}).eq("identificador_ia", tid).execute()
        if tid in pendientes: del pendientes[tid]
    except Exception as e: logger.error(f"Error publicando: {e}")

# ─── Ciclo de Monitoreo ─────────────────────────────────────────────────────
async def monitoreo_wrapper(context: ContextTypes.DEFAULT_TYPE):
    logger.info("🚀 Iniciando escaneo de noticias...")
    encontradas = 0
    for c in CUENTAS_X:
        for item in fetch_tweets_rss(c, num=5):
            if await procesar_noticia(item, context):
                encontradas += 1
                await asyncio.sleep(2)
    logger.info(f"🏁 Escaneo finalizado. Enviadas al panel: {encontradas}")

# ─── Main ───────────────────────────────────────────────────────────────────
def main():
    threading.Thread(target=run_http_server, daemon=True).start()
    app = Application.builder().token(TOKEN).build()
    
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("estado", cmd_estado)) 
    app.add_handler(CommandHandler("scan", cmd_scan))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_input))
    app.add_handler(MessageHandler(filters.PHOTO, recibir_input))
    
    # Escaneo automático cada 15 minutos
    app.job_queue.run_repeating(monitoreo_wrapper, interval=900, first=10)
    
    logger.info("Bot Iniciado...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
