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
logging.basicConfig(level=logging.INFO)
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

# ─── Lógica de Procesamiento (PROMPT ANTI-PUNTOS INVISIBLES) ─────────────────
async def procesar_noticia(n, context):
    tid = hashlib.md5(n["texto"].encode()).hexdigest()[:12]
    try:
        if supabase.table("noticias").select("id").eq("identificador_ia", tid).execute().data: return False
    except: return False

    try:
        completion = client_groq.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": (
                    "Eres el redactor de 'Universo Football'. Estilo profesional.\n\n"
                    "ESTRUCTURA HTML:\n"
                    "🚨🌍 | <b>Titular</b>\n\n"
                    "📝 Hecho 1 (máx 2 líneas).\n"
                    "📝 Hecho 2 (máx 2 líneas).\n\n"
                    "<b>ℹ️ » [Nombre de la Fuente]</b>\n\n"
                    "📲 <b>Suscríbete en t.me/iUniversoFootball</b>\n\n"
                    "REGLAS:\n"
                    "- NO uses el carácter invisible ' ' ni espacios vacíos raros.\n"
                    "- NO escribas la palabra 'Fuente:', solo pon <b>ℹ️ » [Nombre]</b>.\n"
                    "- Si no hay fuente, salta directamente a la firma.\n"
                    "- Emojis SIEMPRE al inicio."
                )},
                {"role": "user", "content": f"Redacta esta noticia limpia: {n['texto']}"}
            ],
            temperature=0.1
        )
        # Limpieza manual post-IA para asegurar que no hay espacios fantasmas
        redac = completion.choices[0].message.content.strip()
        redac = redac.replace('\xa0', '').replace('  ', ' ') # Borra el espacio invisible de Telegram
    except:
        redac = f"📢 <b>NOTICIA</b>\n\n{n['texto']}\n\n📲 <b>Suscríbete en t.me/iUniversoFootball</b>"

    try:
        supabase.table("noticias").insert({"identificador_ia": tid, "url_origen": n["url"], "estado": "pendiente", "texto_final": redac}).execute()
        img_b = None
        if n["img"]:
            try:
                r = requests.get(n["img"], timeout=10)
                if r.status_code == 200: img_b = r.content
            except: pass

        pendientes[tid] = {"texto": redac, "foto": img_b}
        btn = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ PUBLICAR", callback_data=f"p:{tid}"), InlineKeyboardButton("⏰ PROGRAMAR", callback_data=f"s:{tid}")],
            [InlineKeyboardButton("🖼 IMG", callback_data=f"f:{tid}"), InlineKeyboardButton("🗑 BORRAR", callback_data=f"d:{tid}")]
        ])
        cap = f"🆔 <code>{tid}</code>\n\n{redac}"
        if img_b: await context.bot.send_photo(ADMIN_ID, BytesIO(img_b), caption=cap, parse_mode=ParseMode.HTML, reply_markup=btn)
        else: await context.bot.send_message(ADMIN_ID, cap, parse_mode=ParseMode.HTML, reply_markup=btn)
        return True
    except: return False

async def enviar_panel_control(tid, context):
    d = pendientes[tid]
    btn = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ PUBLICAR", callback_data=f"p:{tid}"), InlineKeyboardButton("⏰ PROGRAMAR", callback_data=f"s:{tid}")],
        [InlineKeyboardButton("📝 EDITAR TEXTO", callback_data=f"e:{tid}"), InlineKeyboardButton("🖼 CAMBIAR IMG", callback_data=f"f:{tid}")],
        [InlineKeyboardButton("🗑 BORRAR", callback_data=f"d:{tid}")]
    ])
    cap = f"🆔 <code>{tid}</code>\n\n{d['texto']}"
    if d["foto"]: await context.bot.send_photo(ADMIN_ID, BytesIO(d["foto"]), caption=cap, parse_mode=ParseMode.HTML, reply_markup=btn)
    else: await context.bot.send_message(ADMIN_ID, cap, parse_mode=ParseMode.HTML, reply_markup=btn)

# ─── Handlers de Comandos ────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id == ADMIN_ID:
        await update.message.reply_text("👋 ¡Qué lo qué, Samuel! El bot de <b>Universo Football</b> está listo.\n\n/scan - Buscar noticias\n/estado - Ver salud del bot\n/programados - Ver cola", parse_mode=ParseMode.HTML)

async def cmd_estado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id == ADMIN_ID:
        hora_vln = datetime.now(VENEZUELA_TZ).strftime("%H:%M:%S")
        await update.message.reply_text(f"✅ <b>Bot Online</b>\n📍 Hora Caracas: {hora_vln}\n📦 Pendientes: {len(pendientes)}", parse_mode=ParseMode.HTML)

async def cmd_programados(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id == ADMIN_ID:
        jobs = context.job_queue.jobs()
        if not jobs: return await update.message.reply_text("📭 No hay nada programado.")
        txt = "📅 <b>Próximos Posts (Hora CCS):</b>\n\n"
        for j in jobs:
            if j.name and j.next_t:
                hora = j.next_t.astimezone(VENEZUELA_TZ).strftime("%H:%M")
                txt += f"• {hora} - ID: {j.name}\n"
        await update.message.reply_text(txt, parse_mode=ParseMode.HTML)

# ─── Callbacks y Gestión de Input ──────────────────────────────────────────
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    act, tid = q.data.split(":")
    if tid not in pendientes: return

    if act == "p": await publicar_ahora(tid, context)
    elif act == "s":
        esperando_hora[ADMIN_ID] = tid
        await context.bot.send_message(ADMIN_ID, "⏰ Dime la hora (24h, ej: 14:30):")
    elif act == "e":
        esperando_edicion[ADMIN_ID] = tid
        await context.bot.send_message(ADMIN_ID, "📝 Pásame el nuevo texto (usa HTML):")
    elif act == "f":
        esperando_foto[ADMIN_ID] = tid
        await context.bot.send_message(ADMIN_ID, "📸 Pásame la nueva foto:")
    elif act == "d":
        del pendientes[tid]; await q.delete_message()

async def recibir_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid != ADMIN_ID: return

    if uid in esperando_hora:
        tid = esperando_hora.pop(uid)
        try:
            h, m = map(int, update.message.text.split(":"))
            ahora = datetime.now(VENEZUELA_TZ)
            prog = ahora.replace(hour=h, minute=m, second=0, microsecond=0)
            if prog < ahora: prog += timedelta(days=1)
            context.job_queue.run_once(lambda ctx: publicar_ahora(tid, ctx), when=prog.astimezone(pytz.UTC), name=tid)
            await update.message.reply_text(f"✅ Programado para las {update.message.text} (Hora CCS)")
        except: await update.message.reply_text("❌ Formato inválido.")

    elif uid in esperando_edicion:
        tid = esperando_edicion.pop(uid)
        pendientes[tid]["texto"] = update.message.text_html
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
        if d["foto"]: await context.bot.send_photo(CHANNEL_ID, BytesIO(d["foto"]), caption=d["texto"], parse_mode=ParseMode.HTML)
        else: await context.bot.send_message(CHANNEL_ID, d["texto"], parse_mode=ParseMode.HTML)
        supabase.table("noticias").insert({"identificador_ia": tid, "url_origen": d["url"], "estado": "publicado"}).execute()
        del pendientes[tid]
    except Exception as e: logger.error(e)

# ─── Scanner y Main ──────────────────────────────────────────────────────────
async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id == ADMIN_ID:
        await update.message.reply_text("🔎 Escaneando..."); await monitoreo_wrapper(context)

async def monitoreo_wrapper(context: ContextTypes.DEFAULT_TYPE):
    for c in CUENTAS_X:
        for item in fetch_tweets_rss(c):
            if await procesar_noticia(item, context): await asyncio.sleep(2)

def main():
    threading.Thread(target=run_http_server, daemon=True).start()
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("estado", cmd_estado))
    app.add_handler(CommandHandler("programados", cmd_programados))
    app.add_handler(CommandHandler("scan", cmd_scan))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_input))
    app.add_handler(MessageHandler(filters.PHOTO, recibir_input))
    app.job_queue.run_repeating(monitoreo_wrapper, interval=900, first=10)
    app.run_polling()

if __name__ == "__main__": 
    main()
