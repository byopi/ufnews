"""
╔══════════════════════════════════════════════════════════════╗
║          UNIVERSO FOOTBALL — BOT DE TELEGRAM                 ║
║  Render + Supabase + Gemini 1.5 Flash + APScheduler          ║
╚══════════════════════════════════════════════════════════════╝
Variables de entorno requeridas:
  TELEGRAM_BOT_TOKEN      — Token de @BotFather
  ADMIN_TELEGRAM_ID       — Tu ID numérico
  TELEGRAM_CHANNEL_ID     — @iUniversoFootball
  SUPABASE_URL            — https://XXXXXXXX.supabase.co
  SUPABASE_KEY            — service_role key
  GEMINI_API_KEY          — key de aistudio.google.com
"""

import os
import re
import json
import time
import logging
import threading
import asyncio
import hashlib
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler

import pytz
import requests
from bs4 import BeautifulSoup

import google.generativeai as genai
from supabase import create_client, Client

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, ConversationHandler, filters
)
from telegram.constants import ParseMode

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
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

# ─── Zona horaria Venezuela ────────────────────────────────────────────────────
VE_TZ = pytz.timezone("America/Caracas")

# ─── Variables de entorno ──────────────────────────────────────────────────────
TOKEN          = os.environ["TELEGRAM_BOT_TOKEN"]
ADMIN_ID       = int(os.environ["ADMIN_TELEGRAM_ID"])
CHANNEL_ID     = os.environ["TELEGRAM_CHANNEL_ID"]   # e.g. "@iUniversoFootball"
SUPABASE_URL   = os.environ["SUPABASE_URL"]
SUPABASE_KEY   = os.environ["SUPABASE_KEY"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

# ─── Clientes externos ─────────────────────────────────────────────────────────
genai.configure(api_key=GEMINI_API_KEY)
gemini_model = genai.GenerativeModel("gemini-1.5-flash")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ─── Estados de conversación ───────────────────────────────────────────────────
ESPERANDO_HORA    = 10
ESPERANDO_IMAGEN  = 11

# ─── Estado global en memoria ─────────────────────────────────────────────────
# pendientes[news_id] = {
#   "texto": str,
#   "foto_url": str | None,
#   "foto_bytes": bytes | None,
#   "msg_admin_id": int,
#   "url_origen": str,
# }
pendientes: dict[str, dict] = {}

# ─── HTTP keepalive (igual que tu bot BCV) ─────────────────────────────────────
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
# SCRAPING — Twitter/X via Nitter público
# ═══════════════════════════════════════════════════════════════════════════════

NITTER_INSTANCES = [
    "https://nitter.net",
    "https://nitter.privacydev.net",
    "https://nitter.poast.org",
]

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

def fetch_nitter_tweets(username: str, max_tweets: int = 5) -> list[dict]:
    """Scraping de tweets vía instancias públicas de Nitter."""
    for instance in NITTER_INSTANCES:
        url = f"{instance}/{username}"
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
            if resp.status_code != 200:
                continue
            soup = BeautifulSoup(resp.text, "html.parser")
            tweets = []
            for item in soup.select(".timeline-item")[:max_tweets]:
                # Texto
                content_el = item.select_one(".tweet-content")
                if not content_el:
                    continue
                texto = content_el.get_text(separator=" ", strip=True)
                if not texto or len(texto) < 20:
                    continue
                # URL
                link_el = item.select_one(".tweet-link")
                tweet_url = ""
                if link_el and link_el.get("href"):
                    path = link_el["href"]
                    tweet_url = f"https://x.com{path}" if path.startswith("/") else path
                # Imagen adjunta
                img_url = None
                img_el = item.select_one(".still-image img, .attachment-image img")
                if img_el and img_el.get("src"):
                    src = img_el["src"]
                    if src.startswith("/"):
                        img_url = f"{instance}{src}"
                    else:
                        img_url = src
                tweets.append({
                    "texto": texto,
                    "url": tweet_url or url,
                    "img_url": img_url,
                    "cuenta": username,
                })
            logger.info(f"[Scraper] @{username}: {len(tweets)} tweets via {instance}")
            return tweets
        except Exception as e:
            logger.warning(f"[Scraper] {instance} falló para @{username}: {e}")
            continue
    logger.error(f"[Scraper] Todas las instancias fallaron para @{username}")
    return []

def descargar_imagen(url: str) -> bytes | None:
    """Descarga imagen y devuelve bytes o None."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        if r.status_code == 200 and "image" in r.headers.get("Content-Type", ""):
            return r.content
    except Exception as e:
        logger.warning(f"[Imagen] No se pudo descargar {url}: {e}")
    return None

# ═══════════════════════════════════════════════════════════════════════════════
# GEMINI — Clasificación y redacción
# ═══════════════════════════════════════════════════════════════════════════════

PROMPT_IDENTIFICADOR = """
Analiza este tweet de fútbol y genera un identificador único en formato snake_case
que resuma el evento principal. Máximo 6 palabras separadas por guiones bajos.
Ejemplos: lesion_vinicius_abril, fichaje_mbappe_real_madrid, gol_messi_champions.

Tweet: {texto}

Responde SOLO con el identificador, sin explicación ni comillas.
""".strip()

PROMPT_FICHAJE = """
Eres el redactor oficial de Universo Football, una cuenta de noticias de fútbol en español.
Redacta esta noticia de FICHAJE siguiendo EXACTAMENTE este formato Markdown de Telegram:

✅[BANDERA DEL PAÍS DEL JUGADOR] OFICIAL: [Nombre del jugador] es nuevo jugador del [Club Destino]

➡️ [Cuerpo: de dónde viene, monto o duración del contrato si se menciona, detalles relevantes].

📲 Suscríbete en t.me/iUniversoFootball

REGLAS:
- Usa negritas con *asteriscos* para nombres de jugadores y clubes
- La bandera va justo después del emoji ✅ sin espacio
- Máximo 3 líneas en el cuerpo
- Tono profesional y conciso
- NO inventes datos que no estén en el tweet

Tweet original: {texto}
Fuente: @{cuenta}
""".strip()

PROMPT_NOTICIA = """
Eres el redactor oficial de Universo Football, una cuenta de noticias de fútbol en español.
Redacta esta noticia/información siguiendo EXACTAMENTE este formato Markdown de Telegram:

🚨[BANDERA DEL PAÍS O CLUB RELEVANTE] | [TÍTULO EN MAYÚSCULAS o "ÚLTIMA HORA"]

[Cuerpo con emojis apropiados:
🩺 para lesiones e información médica
👉 para contexto adicional
📊 para estadísticas]

📋 » @{cuenta} [Fuente: X/Twitter]

📲 Suscríbete en t.me/iUniversoFootball

REGLAS:
- Usa negritas con *asteriscos* para nombres de jugadores y clubes
- Máximo 4 líneas en el cuerpo
- Tono urgente y profesional
- NO inventes datos que no estén en el tweet
- Si es lesión, usa 🩺 prominentemente

Tweet original: {texto}
Fuente: @{cuenta}
""".strip()

PROMPT_CLASIFICAR = """
Clasifica este tweet de fútbol. Responde SOLO con una palabra:
- "fichaje" si es sobre un traspaso, fichaje, contratación o transferencia de jugador
- "noticia" si es sobre lesión, declaración, resultado, rumor u otra información

Tweet: {texto}
""".strip()

def gemini_generar(prompt: str) -> str:
    """Llama a Gemini y devuelve el texto generado."""
    try:
        resp = gemini_model.generate_content(prompt)
        return resp.text.strip()
    except Exception as e:
        logger.error(f"[Gemini] Error: {e}")
        return ""

def generar_identificador(texto: str) -> str:
    """Genera identificador único vía Gemini, con fallback a hash."""
    prompt = PROMPT_IDENTIFICADOR.format(texto=texto[:500])
    ia_id = gemini_generar(prompt)
    # Limpiar: solo letras, números y guiones bajos
    ia_id = re.sub(r"[^a-z0-9_]", "", ia_id.lower())[:60]
    if len(ia_id) < 5:
        # Fallback a hash del texto
        ia_id = "noticia_" + hashlib.md5(texto.encode()).hexdigest()[:10]
    return ia_id

def clasificar_tweet(texto: str) -> str:
    """Clasifica el tweet como 'fichaje' o 'noticia'."""
    prompt = PROMPT_CLASIFICAR.format(texto=texto[:500])
    resultado = gemini_generar(prompt).lower()
    return "fichaje" if "fichaje" in resultado else "noticia"

def redactar_noticia(texto: str, cuenta: str, tipo: str) -> str:
    """Redacta la noticia formateada para Telegram."""
    if tipo == "fichaje":
        prompt = PROMPT_FICHAJE.format(texto=texto[:800], cuenta=cuenta)
    else:
        prompt = PROMPT_NOTICIA.format(texto=texto[:800], cuenta=cuenta)
    return gemini_generar(prompt)

# ═══════════════════════════════════════════════════════════════════════════════
# SUPABASE — Persistencia
# ═══════════════════════════════════════════════════════════════════════════════

def ya_existe_en_db(identificador_ia: str) -> bool:
    """Verifica si el identificador ya existe en la tabla noticias."""
    try:
        result = (
            supabase.table("noticias")
            .select("id")
            .eq("identificador_ia", identificador_ia)
            .limit(1)
            .execute()
        )
        return len(result.data) > 0
    except Exception as e:
        logger.error(f"[Supabase] Error al consultar: {e}")
        return False

def guardar_noticia(identificador_ia: str, url_origen: str, tipo: str,
                    estado: str, texto_final: str) -> int | None:
    """Inserta una nueva noticia y devuelve el id generado."""
    try:
        result = (
            supabase.table("noticias")
            .insert({
                "identificador_ia": identificador_ia,
                "url_origen": url_origen,
                "tipo": tipo,
                "estado": estado,
                "texto_final": texto_final,
            })
            .execute()
        )
        if result.data:
            return result.data[0]["id"]
    except Exception as e:
        logger.error(f"[Supabase] Error al insertar: {e}")
    return None

def actualizar_estado(identificador_ia: str, estado: str):
    """Actualiza el campo estado de una noticia."""
    try:
        supabase.table("noticias").update({"estado": estado}).eq(
            "identificador_ia", identificador_ia
        ).execute()
    except Exception as e:
        logger.error(f"[Supabase] Error al actualizar estado: {e}")

# ═══════════════════════════════════════════════════════════════════════════════
# PIPELINE PRINCIPAL — Procesar un tweet
# ═══════════════════════════════════════════════════════════════════════════════

async def procesar_tweet(tweet: dict, app):
    """
    Pipeline completo:
    1. Generar identificador_ia con Gemini
    2. Verificar duplicado en Supabase
    3. Clasificar y redactar
    4. Guardar en DB con estado='pendiente'
    5. Enviar previa al admin con botones
    """
    texto    = tweet["texto"]
    url      = tweet["url"]
    cuenta   = tweet["cuenta"]
    img_url  = tweet.get("img_url")

    # 1. Identificador único
    identificador_ia = generar_identificador(texto)
    logger.info(f"[Pipeline] Identificador: {identificador_ia}")

    # 2. Verificar duplicado
    if ya_existe_en_db(identificador_ia):
        logger.info(f"[Pipeline] Duplicado, ignorando: {identificador_ia}")
        return

    # 3. Clasificar y redactar
    tipo         = clasificar_tweet(texto)
    texto_final  = redactar_noticia(texto, cuenta, tipo)
    if not texto_final:
        logger.warning(f"[Pipeline] Gemini no generó texto para {identificador_ia}")
        return

    # 4. Guardar en DB
    db_id = guardar_noticia(identificador_ia, url, tipo, "pendiente", texto_final)
    logger.info(f"[Pipeline] Guardado en DB con id={db_id}")

    # 5. Descargar imagen si existe
    foto_bytes = descargar_imagen(img_url) if img_url else None

    # 6. Guardar en pendientes
    news_key = identificador_ia
    pendientes[news_key] = {
        "texto": texto_final,
        "foto_bytes": foto_bytes,
        "img_url": img_url,
        "msg_admin_id": None,
        "url_origen": url,
        "identificador_ia": identificador_ia,
    }

    # 7. Enviar previa al admin
    await enviar_previa_admin(app, news_key)

async def enviar_previa_admin(app, news_key: str):
    """Envía la previa al administrador con botones de acción."""
    data = pendientes.get(news_key)
    if not data:
        return

    texto_final  = data["texto"]
    foto_bytes   = data["foto_bytes"]
    identificador = data["identificador_ia"]

    teclado = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Publicar",      callback_data=f"pub:{news_key}"),
            InlineKeyboardButton("🗑 Eliminar",      callback_data=f"del:{news_key}"),
        ],
        [
            InlineKeyboardButton("⏰ Programar",    callback_data=f"sch:{news_key}"),
            InlineKeyboardButton("🖼 Cambiar imagen", callback_data=f"img:{news_key}"),
        ],
    ])

    encabezado = (
        f"🔔 *PREVIA — UNIVERSO FOOTBALL*\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 `{identificador}`\n"
        f"🔗 [Ver tweet]({data['url_origen']})\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{texto_final}"
    )

    try:
        if foto_bytes:
            from io import BytesIO
            msg = await app.bot.send_photo(
                chat_id=ADMIN_ID,
                photo=BytesIO(foto_bytes),
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
        logger.error(f"[Admin] Error al enviar previa: {e}")

# ═══════════════════════════════════════════════════════════════════════════════
# SCHEDULER — Monitoreo periódico
# ═══════════════════════════════════════════════════════════════════════════════

async def tarea_monitoreo(app):
    """Tarea que corre cada ~12 minutos para scrapear todas las cuentas."""
    logger.info("[Scheduler] Iniciando monitoreo de cuentas...")
    for cuenta in CUENTAS_X:
        try:
            tweets = fetch_nitter_tweets(cuenta, max_tweets=3)
            for tweet in tweets:
                await procesar_tweet(tweet, app)
                await asyncio.sleep(2)  # Delay entre tweets
        except Exception as e:
            logger.error(f"[Scheduler] Error procesando @{cuenta}: {e}")
        await asyncio.sleep(5)  # Delay entre cuentas

# ═══════════════════════════════════════════════════════════════════════════════
# HANDLERS — Callbacks de botones del admin
# ═══════════════════════════════════════════════════════════════════════════════

async def publicar_en_canal(app, news_key: str):
    """Publica la noticia en el canal de Telegram."""
    data = pendientes.get(news_key)
    if not data:
        return False
    try:
        foto_bytes = data.get("foto_bytes")
        texto_final = data["texto"]
        if foto_bytes:
            from io import BytesIO
            await app.bot.send_photo(
                chat_id=CHANNEL_ID,
                photo=BytesIO(foto_bytes),
                caption=texto_final[:1024],
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            await app.bot.send_message(
                chat_id=CHANNEL_ID,
                text=texto_final,
                parse_mode=ParseMode.MARKDOWN,
            )
        actualizar_estado(data["identificador_ia"], "publicado")
        del pendientes[news_key]
        logger.info(f"[Canal] Publicado: {news_key}")
        return True
    except Exception as e:
        logger.error(f"[Canal] Error al publicar: {e}")
        return False

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Maneja todos los callbacks de botones inline del admin."""
    query  = update.callback_query
    await query.answer()

    if query.from_user.id != ADMIN_ID:
        await query.answer("⛔ No autorizado.", show_alert=True)
        return

    data = query.data
    partes   = data.split(":", 1)
    accion   = partes[0]
    news_key = partes[1] if len(partes) > 1 else ""

    # ── PUBLICAR ──────────────────────────────────────────────────────────────
    if accion == "pub":
        ok = await publicar_en_canal(context.application, news_key)
        if ok:
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text(f"✅ Publicado en {CHANNEL_ID}")
        else:
            await query.message.reply_text("❌ Error al publicar.")

    # ── ELIMINAR ──────────────────────────────────────────────────────────────
    elif accion == "del":
        if news_key in pendientes:
            identificador = pendientes[news_key]["identificador_ia"]
            actualizar_estado(identificador, "eliminado")
            del pendientes[news_key]
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("🗑 Noticia eliminada.")

    # ── PROGRAMAR ─────────────────────────────────────────────────────────────
    elif accion == "sch":
        context.user_data["sch_news_key"] = news_key
        await query.message.reply_text(
            "⏰ *Programar publicación*\n\n"
            "Envía la hora en formato `HH:MM` (zona horaria Venezuela 🇻🇪)\n"
            "Ejemplo: `18:30`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return ESPERANDO_HORA

    # ── CAMBIAR IMAGEN ────────────────────────────────────────────────────────
    elif accion == "img":
        context.user_data["img_news_key"] = news_key
        await query.message.reply_text(
            "🖼 *Cambiar imagen*\n\n"
            "Envía la nueva foto para esta noticia.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return ESPERANDO_IMAGEN

async def recibir_hora(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Recibe la hora HH:MM y programa la publicación."""
    news_key = context.user_data.get("sch_news_key")
    texto_hora = update.message.text.strip()

    if not re.match(r"^\d{1,2}:\d{2}$", texto_hora):
        await update.message.reply_text("❌ Formato inválido. Usa HH:MM (ej: 18:30)")
        return ESPERANDO_HORA

    hora, minuto = map(int, texto_hora.split(":"))
    ahora_ve = datetime.now(VE_TZ)
    fecha_pub = ahora_ve.replace(hour=hora, minute=minuto, second=0, microsecond=0)
    if fecha_pub <= ahora_ve:
        fecha_pub += timedelta(days=1)

    scheduler: AsyncIOScheduler = context.application.bot_data.get("scheduler")
    if scheduler:
        scheduler.add_job(
            publicar_en_canal,
            trigger=DateTrigger(run_date=fecha_pub, timezone=VE_TZ),
            args=[context.application, news_key],
            id=f"pub_{news_key}",
            replace_existing=True,
        )
    hora_str = fecha_pub.strftime("%d/%m/%Y %H:%M")
    await update.message.reply_text(
        f"✅ Programado para el *{hora_str}* (Venezuela 🇻🇪)",
        parse_mode=ParseMode.MARKDOWN,
    )
    context.user_data.pop("sch_news_key", None)
    return ConversationHandler.END

async def recibir_imagen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Recibe nueva imagen y actualiza la previa."""
    news_key = context.user_data.get("img_news_key")
    if not news_key or news_key not in pendientes:
        await update.message.reply_text("❌ Noticia no encontrada.")
        return ConversationHandler.END

    if not update.message.photo:
        await update.message.reply_text("❌ Debes enviar una foto.")
        return ESPERANDO_IMAGEN

    foto = update.message.photo[-1]  # Mayor resolución
    archivo = await foto.get_file()
    foto_bytes = await archivo.download_as_bytearray()
    pendientes[news_key]["foto_bytes"] = bytes(foto_bytes)

    await update.message.reply_text("✅ Imagen actualizada. Enviando previa actualizada...")
    await enviar_previa_admin(context.application, news_key)
    context.user_data.pop("img_news_key", None)
    return ConversationHandler.END

async def cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Operación cancelada.")
    return ConversationHandler.END

# ─── Comando /estado ──────────────────────────────────────────────────────────
async def cmd_estado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra el estado actual del bot (solo admin)."""
    if update.effective_user.id != ADMIN_ID:
        return
    ahora_ve = datetime.now(VE_TZ).strftime("%d/%m/%Y %H:%M")
    n_pend = len(pendientes)
    msg = (
        f"🤖 *Universo Football Bot*\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🕐 Hora VE: `{ahora_ve}`\n"
        f"📋 Noticias pendientes: `{n_pend}`\n"
        f"📡 Canal: `{CHANNEL_ID}`\n"
        f"✅ Bot activo y funcionando"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

async def cmd_forzar_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fuerza un escaneo inmediato (solo admin)."""
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text("🔍 Iniciando escaneo manual...")
    await tarea_monitoreo(context.application)
    await update.message.reply_text("✅ Escaneo completado.")

async def cmd_pendientes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lista las noticias pendientes (solo admin)."""
    if update.effective_user.id != ADMIN_ID:
        return
    if not pendientes:
        await update.message.reply_text("📭 No hay noticias pendientes.")
        return
    lista = "\n".join([f"• `{k}`" for k in pendientes.keys()])
    await update.message.reply_text(
        f"📋 *Noticias pendientes:*\n{lista}",
        parse_mode=ParseMode.MARKDOWN,
    )

# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

async def post_init(app: Application):
    """Se ejecuta tras inicializar la app — configura el scheduler."""
    scheduler = AsyncIOScheduler(timezone=VE_TZ)

    # Monitoreo cada 12 minutos
    scheduler.add_job(
        tarea_monitoreo,
        trigger="interval",
        minutes=12,
        args=[app],
        id="monitoreo_periodico",
        next_run_time=datetime.now(VE_TZ) + timedelta(seconds=30),
    )

    scheduler.start()
    app.bot_data["scheduler"] = scheduler
    logger.info("[Scheduler] APScheduler iniciado — monitoreo cada 12 minutos.")

    # Notificar al admin
    try:
        await app.bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                "🟢 *Universo Football Bot iniciado*\n\n"
                "Comandos disponibles:\n"
                "/estado — Estado del bot\n"
                "/pendientes — Noticias en espera\n"
                "/scan — Forzar escaneo ahora"
            ),
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception:
        pass

def main():
    # Servidor HTTP en hilo daemon (UptimeRobot keepalive — igual que tu bot BCV)
    threading.Thread(target=run_http_server, daemon=True).start()
    logger.info("[HTTP] Servidor keepalive iniciado.")

    app = (
        Application.builder()
        .token(TOKEN)
        .post_init(post_init)
        .build()
    )

    # ConversationHandler para programar + cambiar imagen
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

    app.add_handler(CommandHandler("estado",      cmd_estado))
    app.add_handler(CommandHandler("scan",        cmd_forzar_scan))
    app.add_handler(CommandHandler("pendientes",  cmd_pendientes))
    app.add_handler(conv)

    logger.info("[Bot] Iniciando polling...")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        poll_interval=2.0,
        timeout=20,
    )

if __name__ == "__main__":
    main()
