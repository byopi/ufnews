import os, hashlib, requests, logging, threading, asyncio, random
from io import BytesIO
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

CUENTAS_X = ["mercatosphera", "Mercado_Ingles", "SoyCalcio_", "postunited", "laligaa_neews"]
pendientes = {}
esperando_foto = {}

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

# ─── Lógica de Procesamiento ──────────────────────────────────────────────────
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
                    "Eres el redactor jefe de 'Universo Football'. Estilo profesional y visual.\n\n"
                    "FORMATO HTML ESTRICTO:\n"
                    "1. Titular en negrita con emojis AL INICIO (Ej: 🚨🇪🇸 | <b>Titular de la noticia</b>)\n"
                    "2. Salto de línea SIMPLE.\n\n"
                    "3. Cuerpo: Dos hechos descriptivos. Cada hecho debe tener exactamente 2 líneas de texto para que se vea estético. "
                    "Los emojis SIEMPRE AL INICIO del párrafo (Ej: 📝 Texto largo que rellene dos líneas...).\n"
                    "4. Entre el primer hecho y el segundo, deja un SALTO DE LÍNEA SIMPLE.\n\n"
                    "5. FUENTE: Solo si hay una fuente clara, ponla en negrita: <b>ℹ️ » [Nombre]</b>. Si no hay fuente, NO PONGAS NADA.\n"
                    "6. Firma en negrita: 📲 <b>Suscríbete en t.me/iUniversoFootball</b>\n\n"
                    "REGLAS:\n"
                    "- Los hechos deben ser coherentes y detallados (no frases cortas de 3 palabras).\n"
                    "- Usa saltos de línea para separar secciones como el ejemplo del usuario.\n"
                    "- Prohibido usar asteriscos, solo etiquetas <b></b>."
                )},
                {"role": "user", "content": f"Parafrasea esta noticia con hechos de 2 líneas cada uno: {n['texto']}"}
            ],
            temperature=0.2
        )
        redac = completion.choices[0].message.content.strip()
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
            [InlineKeyboardButton("✅ PUBLICAR", callback_data=f"p:{tid}"), InlineKeyboardButton("🗑 BORRAR", callback_data=f"d:{tid}")],
            [InlineKeyboardButton("🖼 CAMBIAR IMG", callback_data=f"f:{tid}")]
        ])
        cap = f"🆔 <code>{tid}</code>\n\n{redac}"
        if img_b: await context.bot.send_photo(ADMIN_ID, BytesIO(img_b), caption=cap, parse_mode=ParseMode.HTML, reply_markup=btn)
        else: await context.bot.send_message(ADMIN_ID, cap, parse_mode=ParseMode.HTML, reply_markup=btn)
        return True
    except: return False

# ─── Handlers e Interfaz ─────────────────────────────────────────────────────
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    act, tid = q.data.split(":")
    if tid not in pendientes: return

    if act == "p":
        d = pendientes[tid]
        if d["foto"]: await context.bot.send_photo(CHANNEL_ID, BytesIO(d["foto"]), caption=d["texto"], parse_mode=ParseMode.HTML)
        else: await context.bot.send_message(CHANNEL_ID, d["texto"], parse_mode=ParseMode.HTML)
        supabase.table("noticias").update({"estado": "publicado"}).eq("identificador_ia", tid).execute()
        del pendientes[tid]
        await q.edit_message_reply_markup(None)
    elif act == "d":
        del pendientes[tid]; await q.delete_message()
    elif act == "f":
        esperando_foto[ADMIN_ID] = tid
        await context.bot.send_message(ADMIN_ID, "📸 Pásame la nueva foto:")

async def recibir_foto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID or ADMIN_ID not in esperando_foto: return
    tid = esperando_foto.pop(ADMIN_ID)
    foto = await update.message.photo[-1].get_file()
    f_byte = await foto.download_as_bytearray()
    if tid in pendientes:
        pendientes[tid]["foto"] = bytes(f_byte)
        await update.message.reply_text(f"✅ Foto actualizada.", parse_mode=ParseMode.HTML)

async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id == ADMIN_ID:
        await update.message.reply_text("🔎 Escaneando..."); context.job_queue.run_once(monitoreo_wrapper, when=0)

async def monitoreo_wrapper(context: ContextTypes.DEFAULT_TYPE):
    for c in CUENTAS_X:
        items = fetch_tweets_rss(c)
        for item in items:
            if await procesar_noticia(item, context): await asyncio.sleep(2)

def main():
    threading.Thread(target=run_http_server, daemon=True).start()
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("scan", cmd_scan))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.PHOTO, recibir_foto))
    app.job_queue.run_repeating(monitoreo_wrapper, interval=900, first=10)
    app.run_polling()

if __name__ == "__main__": 
    main()
