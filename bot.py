"""
╔══════════════════════════════════════════════════════════════╗
║          UNIVERSO FOOTBALL — BOT DE TELEGRAM                 ║
║  Render + Supabase + Gemini 1.5 Flash + APScheduler          ║
╚══════════════════════════════════════════════════════════════╝
"""

import os
import re
import json
import logging
import threading
import asyncio
import hashlib
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from io import BytesIO

import pytz
import requests
import feedparser
from bs4 import BeautifulSoup

import google.generativeai as genai
from supabase import create_client, Client

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, ConversationHandler, filters
)
from telegram.constants import ParseMode

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("universo_football")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)

VE_TZ = pytz.timezone("America/Caracas")

TOKEN          = os.environ["TELEGRAM_BOT_TOKEN"]
ADMIN_ID       = int(os.environ["ADMIN_TELEGRAM_ID"])
CHANNEL_ID     = os.environ["TELEGRAM_CHANNEL_ID"]
SUPABASE_URL   = os.environ["SUPABASE_URL"]
SUPABASE_KEY   = os.environ["SUPABASE_KEY"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

genai.configure(api_key=GEMINI_API_KEY)
gemini_model = genai.GenerativeModel("gemini-1.5-flash")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

ESPERANDO_HORA   = 10
ESPERANDO_IMAGEN = 11

pendientes: dict[str, dict] = {}

CUENTAS_X = [
    "mercatosphera",
    "Mercado_Ingles",
    "SoyCalcio_",
    "postunited",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
}

# ═══════════════════════════════════════════════════════════════════════════════
# HTTP KEEPALIVE
# ═══════════════════════════════════════════════════════════════════════════════

class PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Universo Football Bot - OK")
    def log_message(self, *args):
        pass

def run_http_server():
    port = int(os.environ.get("PORT", 8080))
    HTTPServer(("0.0.0.0", port), PingHandler).serve_forever()

# ═══════════════════════════════════════════════════════════════════════════════
# SCRAPING — Cascada: RSSHub → Syndication API → Twiiit
# ═══════════════════════════════════════════════════════════════════════════════

RSS_HUB_INSTANCES = [
    "https://rsshub.app/twitter/user/{user}",
    "https://rsshub.rssforever.com/twitter/user/{user}",
    "https://hub.slarker.me/twitter/user/{user}",
]


def fetch_via_rsshub(username: str, max_tweets: int = 5) -> list[dict]:
    """Método 1: RSSHub — convierte perfiles de X en RSS sin API."""
    for template in RSS_HUB_INSTANCES:
        url = template.format(user=username)
        try:
            feed = feedparser.parse(url)
            if not feed.entries:
                continue
            tweets = []
            for entry in feed.entries[:max_tweets]:
                raw   = entry.get("summary", entry.get("title", ""))
                texto = BeautifulSoup(raw, "html.parser").get_text(separator=" ", strip=True)
                if not texto or len(texto) < 15:
                    continue
                tweet_url = entry.get("link", f"https://x.com/{username}")
                img_url = None
                for media in entry.get("media_content", []):
                    if media.get("url"):
                        img_url = media["url"]
                        break
                if not img_url:
                    for enc in entry.get("enclosures", []):
                        if "image" in enc.get("type", ""):
                            img_url = enc.get("href") or enc.get("url")
                            break
                tweets.append({"texto": texto, "url": tweet_url,
                                "img_url": img_url, "cuenta": username})
            if tweets:
                logger.info(f"[RSSHub] @{username}: {len(tweets)} tweets via {url}")
                return tweets
        except Exception as e:
            logger.warning(f"[RSSHub] {url} falló: {e}")
    return []


def fetch_via_syndication(username: str, max_tweets: int = 5) -> list[dict]:
    """Método 2: Twitter Syndication API (usada por widgets embebidos, sin auth)."""
    url = f"https://syndication.twitter.com/srv/timeline-profile/screen-name/{username}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            return []
        soup   = BeautifulSoup(resp.text, "html.parser")
        script = soup.find("script", {"id": "__NEXT_DATA__"})
        if not script:
            return []
        data = json.loads(script.string)
        try:
            entries = data["props"]["pageProps"]["timeline"]["entries"]
        except (KeyError, TypeError):
            return []
        tweets = []
        for entry in entries[:max_tweets]:
            try:
                td    = entry["content"]["tweet"]
                texto = td.get("full_text") or td.get("text", "")
                if not texto or len(texto) < 15:
                    continue
                tid       = td.get("id_str", "")
                tweet_url = f"https://x.com/{username}/status/{tid}"
                img_url   = None
                media_list = td.get("entities", {}).get("media", [])
                if media_list:
                    img_url = media_list[0].get("media_url_https")
                tweets.append({"texto": texto, "url": tweet_url,
                                "img_url": img_url, "cuenta": username})
            except (KeyError, TypeError):
                continue
        if tweets:
            logger.info(f"[Syndication] @{username}: {len(tweets)} tweets")
        return tweets
    except Exception as e:
        logger.warning(f"[Syndication] @{username}: {e}")
        return []


def fetch_via_twiiit(username: str, max_tweets: int = 5) -> list[dict]:
    """Método 3: Twiiit.com — proxy de Twitter con HTML scrapeable."""
    url = f"https://twiiit.com/{username}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            return []
        soup   = BeautifulSoup(resp.text, "html.parser")
        tweets = []
        for item in soup.select(".timeline-item")[:max_tweets]:
            content_el = item.select_one(".tweet-content")
            if not content_el:
                continue
            texto = content_el.get_text(separator=" ", strip=True)
            if not texto or len(texto) < 15:
                continue
            link_el   = item.select_one("a.tweet-link")
            tweet_url = ""
            if link_el:
                href      = link_el.get("href", "")
                tweet_url = f"https://x.com{href}" if href.startswith("/") else href
            img_url = None
            img_el  = item.select_one(".still-image img, .attachment-image img")
            if img_el:
                src     = img_el.get("src", "")
                img_url = f"https://twiiit.com{src}" if src.startswith("/") else src
            tweets.append({"texto": texto, "url": tweet_url or url,
                            "img_url": img_url, "cuenta": username})
        if tweets:
            logger.info(f"[Twiiit] @{username}: {len(tweets)} tweets")
        return tweets
    except Exception as e:
        logger.warning(f"[Twiiit] @{username}: {e}")
        return []


def fetch_nitter_tweets(username: str, max_tweets: int = 5) -> list[dict]:
    """Orquestador en cascada: RSSHub → Syndication → Twiiit."""
    for fn in (fetch_via_rsshub, fetch_via_syndication, fetch_via_twiiit):
        result = fn(username, max_tweets)
        if result:
            return result
    logger.error(f"[Scraper] Todos los métodos fallaron para @{username}")
    return []


def descargar_imagen(url: str) -> bytes | None:
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        if r.status_code == 200 and "image" in r.headers.get("Content-Type", ""):
            return r.content
    except Exception as e:
        logger.warning(f"[Imagen] {url}: {e}")
    return None

# ═══════════════════════════════════════════════════════════════════════════════
# GEMINI
# ═══════════════════════════════════════════════════════════════════════════════

PROMPT_IDENTIFICADOR = (
    "Analiza este tweet de fútbol y genera un identificador único en snake_case. "
    "Máximo 6 palabras. Ejemplos: lesion_vinicius_abril, fichaje_mbappe_real_madrid.\n"
    "Tweet: {texto}\n"
    "Responde SOLO con el identificador, sin comillas ni explicación."
)

PROMPT_CLASIFICAR = (
    "Clasifica este tweet. Responde SOLO 'fichaje' o 'noticia'.\n"
    "- fichaje: traspaso, transferencia, contratación, firma de jugador\n"
    "- noticia: lesión, declaración, resultado, rumor, cualquier otra info\n"
    "Tweet: {texto}"
)

PROMPT_FICHAJE = (
    "Eres el redactor oficial de Universo Football. "
    "Redacta esta noticia de FICHAJE con EXACTAMENTE este formato Telegram Markdown:\n\n"
    "✅[BANDERA] OFICIAL: *[Jugador]* es nuevo jugador del *[Club Destino]*\n\n"
    "➡️ [Procedencia, costo o duración del contrato — máximo 2 líneas].\n\n"
    "📲 Suscríbete en t.me/iUniversoFootball\n\n"
    "REGLAS: Negritas con *asteriscos*. NO inventes datos. Tono profesional.\n"
    "Tweet: {texto}\nFuente: @{cuenta}"
)

PROMPT_NOTICIA = (
    "Eres el redactor oficial de Universo Football. "
    "Redacta esta noticia con EXACTAMENTE este formato Telegram Markdown:\n\n"
    "🚨[BANDERA] | [TÍTULO EN MAYÚSCULAS]\n\n"
    "[Cuerpo con emojis: 🩺 lesiones · 👉 contexto · 📊 estadísticas — máximo 3 líneas]\n\n"
    "📋 » @{cuenta} [X/Twitter]\n\n"
    "📲 Suscríbete en t.me/iUniversoFootball\n\n"
    "REGLAS: Negritas con *asteriscos*. NO inventes datos. Tono urgente.\n"
    "Tweet: {texto}"
)


def gemini_generar(prompt: str) -> str:
    try:
        return gemini_model.generate_content(prompt).text.strip()
    except Exception as e:
        logger.error(f"[Gemini] {e}")
        return ""


def generar_identificador(texto: str) -> str:
    ia_id = gemini_generar(PROMPT_IDENTIFICADOR.format(texto=texto[:500]))
    ia_id = re.sub(r"[^a-z0-9_]", "", ia_id.lower())[:60]
    return ia_id if len(ia_id) >= 5 else "noticia_" + hashlib.md5(texto.encode()).hexdigest()[:10]


def clasificar_tweet(texto: str) -> str:
    res = gemini_generar(PROMPT_CLASIFICAR.format(texto=texto[:500])).lower()
    return "fichaje" if "fichaje" in res else "noticia"


def redactar_noticia(texto: str, cuenta: str, tipo: str) -> str:
    prompt = (PROMPT_FICHAJE if tipo == "fichaje" else PROMPT_NOTICIA)
    return gemini_generar(prompt.format(texto=texto[:800], cuenta=cuenta))

# ═══════════════════════════════════════════════════════════════════════════════
# SUPABASE
# ═══════════════════════════════════════════════════════════════════════════════

def ya_existe_en_db(identificador_ia: str) -> bool:
    try:
        r = (supabase.table("noticias")
             .select("id").eq("identificador_ia", identificador_ia)
             .limit(1).execute())
        return len(r.data) > 0
    except Exception as e:
        logger.error(f"[Supabase] {e}")
        return False


def guardar_noticia(identificador_ia, url_origen, tipo, estado, texto_final):
    try:
        r = supabase.table("noticias").insert({
            "identificador_ia": identificador_ia,
            "url_origen": url_origen,
            "tipo": tipo,
            "estado": estado,
            "texto_final": texto_final,
        }).execute()
        return r.data[0]["id"] if r.data else None
    except Exception as e:
        logger.error(f"[Supabase] insertar: {e}")
        return None


def actualizar_estado(identificador_ia: str, estado: str):
    try:
        supabase.table("noticias").update({"estado": estado}).eq(
            "identificador_ia", identificador_ia).execute()
    except Exception as e:
        logger.error(f"[Supabase] actualizar: {e}")

# ═══════════════════════════════════════════════════════════════════════════════
# PIPELINE
# ═══════════════════════════════════════════════════════════════════════════════

async def procesar_tweet(tweet: dict, app):
    texto   = tweet["texto"]
    url     = tweet["url"]
    cuenta  = tweet["cuenta"]
    img_url = tweet.get("img_url")

    identificador_ia = generar_identificador(texto)
    logger.info(f"[Pipeline] ID: {identificador_ia}")

    if ya_existe_en_db(identificador_ia):
        logger.info(f"[Pipeline] Duplicado ignorado: {identificador_ia}")
        return

    tipo        = clasificar_tweet(texto)
    texto_final = redactar_noticia(texto, cuenta, tipo)
    if not texto_final:
        return

    guardar_noticia(identificador_ia, url, tipo, "pendiente", texto_final)
    foto_bytes = descargar_imagen(img_url) if img_url else None

    pendientes[identificador_ia] = {
        "texto": texto_final,
        "foto_bytes": foto_bytes,
        "img_url": img_url,
        "msg_admin_id": None,
        "url_origen": url,
        "identificador_ia": identificador_ia,
    }
    await enviar_previa_admin(app, identificador_ia)


async def enviar_previa_admin(app, news_key: str):
    data = pendientes.get(news_key)
    if not data:
        return
    teclado = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Publicar",       callback_data=f"pub:{news_key}"),
            InlineKeyboardButton("🗑 Eliminar",       callback_data=f"del:{news_key}"),
        ],
        [
            InlineKeyboardButton("⏰ Programar",     callback_data=f"sch:{news_key}"),
            InlineKeyboardButton("🖼 Cambiar imagen", callback_data=f"img:{news_key}"),
        ],
    ])
    encabezado = (
        f"🔔 *PREVIA — UNIVERSO FOOTBALL*\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 `{news_key}`\n"
        f"🔗 [Ver tweet]({data['url_origen']})\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{data['texto']}"
    )
    try:
        if data.get("foto_bytes"):
            msg = await app.bot.send_photo(
                chat_id=ADMIN_ID,
                photo=BytesIO(data["foto_bytes"]),
                caption=encabezado[:1024],
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=teclado,
            )
        else:
            msg = await app.bot.send_message(
                chat_id=ADMIN_ID,
                text=encabezado,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=teclado,
                disable_web_page_preview=False,
            )
        pendientes[news_key]["msg_admin_id"] = msg.message_id
        logger.info(f"[Admin] Previa enviada: {news_key}")
    except Exception as e:
        logger.error(f"[Admin] {e}")

# ═══════════════════════════════════════════════════════════════════════════════
# SCHEDULER
# ═══════════════════════════════════════════════════════════════════════════════

async def tarea_monitoreo(app):
    logger.info("[Scheduler] Monitoreando cuentas...")
    for cuenta in CUENTAS_X:
        try:
            tweets = fetch_nitter_tweets(cuenta, max_tweets=3)
            for tweet in tweets:
                await procesar_tweet(tweet, app)
                await asyncio.sleep(3)
        except Exception as e:
            logger.error(f"[Scheduler] @{cuenta}: {e}")
        await asyncio.sleep(5)

# ═══════════════════════════════════════════════════════════════════════════════
# HANDLERS
# ═══════════════════════════════════════════════════════════════════════════════

async def publicar_en_canal(app, news_key: str) -> bool:
    data = pendientes.get(news_key)
    if not data:
        return False
    try:
        if data.get("foto_bytes"):
            await app.bot.send_photo(
                chat_id=CHANNEL_ID,
                photo=BytesIO(data["foto_bytes"]),
                caption=data["texto"][:1024],
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            await app.bot.send_message(
                chat_id=CHANNEL_ID,
                text=data["texto"],
                parse_mode=ParseMode.MARKDOWN,
            )
        actualizar_estado(data["identificador_ia"], "publicado")
        del pendientes[news_key]
        return True
    except Exception as e:
        logger.error(f"[Canal] {e}")
        return False


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID:
        await query.answer("⛔ No autorizado.", show_alert=True)
        return

    partes   = query.data.split(":", 1)
    accion   = partes[0]
    news_key = partes[1] if len(partes) > 1 else ""

    if accion == "pub":
        ok = await publicar_en_canal(context.application, news_key)
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(
            f"✅ Publicado en {CHANNEL_ID}" if ok else "❌ Error al publicar."
        )

    elif accion == "del":
        if news_key in pendientes:
            actualizar_estado(pendientes[news_key]["identificador_ia"], "eliminado")
            del pendientes[news_key]
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("🗑 Noticia eliminada.")

    elif accion == "sch":
        context.user_data["sch_news_key"] = news_key
        await query.message.reply_text(
            "⏰ *Programar publicación*\n\nEnvía la hora en formato `HH:MM` (Venezuela 🇻🇪)\nEjemplo: `18:30`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return ESPERANDO_HORA

    elif accion == "img":
        context.user_data["img_news_key"] = news_key
        await query.message.reply_text("🖼 Envía la nueva foto para esta noticia.")
        return ESPERANDO_IMAGEN


async def recibir_hora(update: Update, context: ContextTypes.DEFAULT_TYPE):
    news_key  = context.user_data.get("sch_news_key")
    texto_hora = update.message.text.strip()
    if not re.match(r"^\d{1,2}:\d{2}$", texto_hora):
        await update.message.reply_text("❌ Usa formato HH:MM (ej: 18:30)")
        return ESPERANDO_HORA
    hora, minuto = map(int, texto_hora.split(":"))
    ahora_ve  = datetime.now(VE_TZ)
    fecha_pub = ahora_ve.replace(hour=hora, minute=minuto, second=0, microsecond=0)
    if fecha_pub <= ahora_ve:
        fecha_pub += timedelta(days=1)
    scheduler = context.application.bot_data.get("scheduler")
    if scheduler:
        scheduler.add_job(
            publicar_en_canal,
            trigger=DateTrigger(run_date=fecha_pub, timezone=VE_TZ),
            args=[context.application, news_key],
            id=f"pub_{news_key}",
            replace_existing=True,
        )
    await update.message.reply_text(
        f"✅ Programado para las *{fecha_pub.strftime('%d/%m/%Y %H:%M')}* 🇻🇪",
        parse_mode=ParseMode.MARKDOWN,
    )
    context.user_data.pop("sch_news_key", None)
    return ConversationHandler.END


async def recibir_imagen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    news_key = context.user_data.get("img_news_key")
    if not news_key or news_key not in pendientes:
        await update.message.reply_text("❌ Noticia no encontrada.")
        return ConversationHandler.END
    if not update.message.photo:
        await update.message.reply_text("❌ Debes enviar una foto.")
        return ESPERANDO_IMAGEN
    foto    = update.message.photo[-1]
    archivo = await foto.get_file()
    fb      = await archivo.download_as_bytearray()
    pendientes[news_key]["foto_bytes"] = bytes(fb)
    await update.message.reply_text("✅ Imagen actualizada. Generando previa...")
    await enviar_previa_admin(context.application, news_key)
    context.user_data.pop("img_news_key", None)
    return ConversationHandler.END


async def cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Operación cancelada.")
    return ConversationHandler.END


async def cmd_estado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    ahora_ve = datetime.now(VE_TZ).strftime("%d/%m/%Y %H:%M")
    await update.message.reply_text(
        f"🤖 *Universo Football Bot*\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🕐 Hora VE: `{ahora_ve}`\n"
        f"📋 Pendientes: `{len(pendientes)}`\n"
        f"📡 Canal: `{CHANNEL_ID}`\n"
        f"✅ Activo y funcionando",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text("🔍 Escaneo manual iniciado...")
    await tarea_monitoreo(context.application)
    await update.message.reply_text("✅ Escaneo completado.")


async def cmd_pendientes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not pendientes:
        await update.message.reply_text("📭 No hay noticias pendientes.")
        return
    lista = "\n".join([f"• `{k}`" for k in pendientes])
    await update.message.reply_text(
        f"📋 *Pendientes:*\n{lista}", parse_mode=ParseMode.MARKDOWN
    )

# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

async def post_init(app: Application):
    scheduler = AsyncIOScheduler(timezone=VE_TZ)
    scheduler.add_job(
        tarea_monitoreo,
        trigger="interval",
        minutes=12,
        args=[app],
        id="monitoreo",
        next_run_time=datetime.now(VE_TZ) + timedelta(seconds=20),
    )
    scheduler.start()
    app.bot_data["scheduler"] = scheduler
    logger.info("[Scheduler] Iniciado — cada 12 minutos.")
    try:
        await app.bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                "🟢 *Universo Football Bot iniciado*\n\n"
                "/estado — Estado del bot\n"
                "/pendientes — Noticias en espera\n"
                "/scan — Forzar escaneo ahora"
            ),
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception:
        pass


def main():
    threading.Thread(target=run_http_server, daemon=True).start()
    logger.info("[HTTP] Servidor keepalive iniciado.")

    app = Application.builder().token(TOKEN).post_init(post_init).build()

    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(callback_handler)],
        states={
            ESPERANDO_HORA:   [MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_hora)],
            ESPERANDO_IMAGEN: [MessageHandler(filters.PHOTO, recibir_imagen)],
        },
        fallbacks=[
            CommandHandler("cancelar", cancelar),
            CallbackQueryHandler(callback_handler),
        ],
        per_message=False,
    )

    app.add_handler(CommandHandler("estado",     cmd_estado))
    app.add_handler(CommandHandler("scan",       cmd_scan))
    app.add_handler(CommandHandler("pendientes", cmd_pendientes))
    app.add_handler(conv)

    logger.info("[Bot] Iniciando polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, poll_interval=2.0, timeout=20)


if __name__ == "__main__":
    main()
