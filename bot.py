import os, hashlib, requests, logging, threading, asyncio, random, re, json
from io import BytesIO
from datetime import datetime, timedelta
from typing import Optional
import pytz
from http.server import HTTPServer, BaseHTTPRequestHandler
from groq import Groq
from supabase import create_client, Client
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, MessageHandler, filters
)
from telegram.constants import ParseMode
from bs4 import BeautifulSoup
import feedparser
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ─── Configuración ──────────────────────────────────────────────────────────
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger("universo_football")

TOKEN        = os.environ.get("TELEGRAM_BOT_TOKEN")
ADMIN_ID     = int(os.environ.get("ADMIN_TELEGRAM_ID", 0))
CHANNEL_ID   = os.environ.get("TELEGRAM_CHANNEL_ID")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")

client_groq = Groq(api_key=GROQ_API_KEY)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

VENEZUELA_TZ = pytz.timezone('America/Caracas')

CUENTAS_X = ["mercatosphera", "Mercado_Ingles", "SoyCalcio_", "postunited", "laligaa_neews"]

# ─── Sub-usuarios (invitados) ───────────────────────────────────────────────
# Permite ceder UNA cuenta de X de CUENTAS_X a otra persona (con su propio
# Telegram ID y su propio canal). Las noticias detectadas en esa cuenta se le
# envían solo a ella y solo puede publicar/editar/programar/borrar SUS propias
# noticias pendientes; nunca ve ni toca las del admin principal.
#
# Variables de entorno necesarias para activar un invitado:
#   FRIEND_TELEGRAM_ID -> ID numérico de Telegram del invitado (@userinfobot)
#   FRIEND_CHANNEL_ID  -> Canal/chat destino del invitado (ej: @canal_amigo)
#   FRIEND_CUENTA_X    -> Nombre exacto de la cuenta en CUENTAS_X que le pertenece
FRIEND_ID         = int(os.environ.get("FRIEND_TELEGRAM_ID", 0) or 0)
FRIEND_CHANNEL_ID = os.environ.get("FRIEND_CHANNEL_ID")
FRIEND_CUENTA_X   = os.environ.get("FRIEND_CUENTA_X", "Mercado_Ingles")
FRIEND_CANAL_TEXTO = os.environ.get("FRIEND_CANAL_TEXTO", "t.me/PremierLeagueES")

SUB_USUARIOS = {}
if FRIEND_ID and FRIEND_CHANNEL_ID and FRIEND_CUENTA_X:
    if FRIEND_CUENTA_X not in CUENTAS_X:
        logger.warning(f"FRIEND_CUENTA_X='{FRIEND_CUENTA_X}' no está en CUENTAS_X; el invitado no recibirá noticias.")
    SUB_USUARIOS[FRIEND_CUENTA_X] = {
        "user_id": FRIEND_ID,
        "channel_id": FRIEND_CHANNEL_ID,
        "canal_texto": FRIEND_CANAL_TEXTO,
    }

USUARIOS_AUTORIZADOS = {ADMIN_ID} | {v["user_id"] for v in SUB_USUARIOS.values()}

def obtener_destino(cuenta: str) -> dict:
    """Devuelve el dueño (Telegram ID), el canal destino y el texto de
    suscripción para una cuenta de X. Si la cuenta no tiene invitado
    asignado, pertenece al admin principal."""
    return SUB_USUARIOS.get(cuenta, {
        "user_id": ADMIN_ID,
        "channel_id": CHANNEL_ID,
        "canal_texto": "t.me/iUniversoFootball",
    })

# Diagnóstico al arrancar: para que sea evidente en los logs de Render si el
# invitado quedó bien configurado o si falta algo.
if SUB_USUARIOS:
    for cuenta, info in SUB_USUARIOS.items():
        logger.info(
            f"👥 Invitado activo -> cuenta X: '{cuenta}' | telegram_id: {info['user_id']} | "
            f"canal: {info['channel_id']} | texto: {info['canal_texto']}"
        )
else:
    faltantes = []
    if not FRIEND_ID: faltantes.append("FRIEND_TELEGRAM_ID")
    if not FRIEND_CHANNEL_ID: faltantes.append("FRIEND_CHANNEL_ID")
    if not FRIEND_CUENTA_X: faltantes.append("FRIEND_CUENTA_X")
    if faltantes:
        logger.warning(f"⚠️ Invitado NO activo. Faltan variables de entorno: {', '.join(faltantes)}")

pendientes    = {}
esperando_foto = {}
esperando_hora = {}
esperando_edicion = {}

# ─── GIF para partidos ──────────────────────────────────────────────────────
GIF_URL = (
    "https://blogger.googleusercontent.com/img/b/R29vZ2xl/"
    "AVvXsEhgjGA2lzs-pgUhRrGYImfMvrjRFkGnili3j9_rSSnll0F83NELGw0q3zqjJtPJ1Wcb7aPq5KS2wtfBn"
    "DZTre8V1swHgrJ1Ec_I-087cInEOsic_6sbaTqsEx0UGUlY97w8vh1zU5RzjsXNSfBXIlmTmDOWrdo4oE8nux"
    "kxHSkP33y4Lard0BsQGvV3kGM/s600/doc_2026-03-04_19-31-59.gif"
)

# ─── Ligas ESPN ─────────────────────────────────────────────────────────────
ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer"

LEAGUES = {
    "ger.1":                        ("🇩🇪", "Bundesliga"),
    "ger.dfb_pokal":                ("🇩🇪", "DFB-Pokal"),
    "esp.1":                        ("🇪🇸", "LaLiga EA Sports"),
    "esp.copa_del_rey":             ("🇪🇸", "Copa del Rey"),
    "esp.super_cup":                ("🇪🇸", "Supercopa de España"),
    "fra.1":                        ("🇫🇷", "Ligue 1"),
    "fra.coupe_de_france":          ("🇫🇷", "Copa de Francia"),
    "eng.1":                        ("🇬🇧", "Premier League"),
    "eng.fa":                       ("🇬🇧", "FA Cup"),
    "eng.league_cup":               ("🇬🇧", "EFL Cup"),
    "eng.community_shield":         ("🇬🇧", "Community Shield"),
    "ita.1":                        ("🇮🇹", "Serie A"),
    "ita.coppa_italia":             ("🇮🇹", "Copa Italia"),
    "uefa.champions":               ("🌍", "Champions League"),
    "uefa.europa":                  ("🌍", "Europa League"),
    "uefa.europa.conf":             ("🌍", "Conference League"),
    "uefa.nations":                 ("🌍", "Nations League"),
    "fifa.worldq.uefa":             ("🇪🇺", "Eliminatorias UEFA"),
    "fifa.worldq.intercontinental": ("🌍", "Repesca Intercontinental"),
    "fifa.friendly":                ("🌍", "Amistosos Internacionales"),
    "international.friendly":       ("🌍", "Amistosos Internacionales"),
    "fifa.world":                   ("🌍", "Mundial FIFA"),
    "uefa.euro":                    ("🇪🇺", "Eurocopa"),
    "conmebol.america":             ("🌎", "Copa América"),
    "caf.nations":                  ("🌍", "Copa Africana de Naciones"),
    "fifa.series":                  ("🌍", "FIFA Series"),
    "fifa.series.men":              ("🌍", "FIFA Series"),
    "fifa.worldq.conmebol":         ("🌎", "Eliminatorias CONMEBOL"),
    "conmebol.qualifying":          ("🌎", "Eliminatorias CONMEBOL"),
    "uefa.qualifying":              ("🇪🇺", "Repesca Europea"),
    "fifa.worldq.concacaf":         ("🌎", "Eliminatorias CONCACAF"),
    "concacaf.qualifying":          ("🌎", "Eliminatorias CONCACAF"),
    "fifa.worldq.afc":              ("🌏", "Eliminatorias AFC"),
    "fifa.worldq.caf":              ("🌍", "Eliminatorias CAF"),
    "fifa.worldq.afc.conmebol":     ("🌍", "Repesca AFC/CONMEBOL"),
    "conmebol.libertadores":        ("🌎", "CONMEBOL Libertadores"),
    "conmebol.sudamericana":        ("🌎", "CONMEBOL Sudamericana"),
    "conmebol.recopa":              ("🌎", "Recopa Sudamericana"),
    "fifa.intercontinental":        ("🌍", "FIFA Intercontinental Cup"),
}

ROUND_TRANSLATIONS = {
    "1st leg": "Ida",
    "2nd leg": "Vuelta",
    "round of 16": "Octavos de Final",
    "round of 32": "Dieciseisavos de Final",
    "quarterfinals": "Cuartos de Final",
    "semifinals": "Semifinales",
    "final": "Final",
    "third place": "Tercer Puesto",
    "group stage": "Fase de Grupos",
    "playoff": "Playoff",
    "qualifying": "Clasificación",
    "extra time": "Prórroga",
}

def translate_round(text: str) -> str:
    if not text:
        return ""
    lower = text.strip().lower()
    for eng, esp in ROUND_TRANSLATIONS.items():
        if eng in lower:
            return esp
    return text.strip()

def get_round_name(event: dict) -> str:
    try:
        competitions = event.get("competitions", [])
        comp = competitions[0] if competitions else {}
        notes = comp.get("notes", [])
        if notes and isinstance(notes, list) and isinstance(notes[0], dict):
            headline = notes[0].get("headline", "")
            if headline:
                return translate_round(headline)
        week = event.get("week", {})
        if isinstance(week, dict):
            number = week.get("number")
            if number:
                return f"Jornada {number}"
        season = event.get("season", {})
        if isinstance(season, dict):
            stype = season.get("type", {})
            if isinstance(stype, dict):
                desc = stype.get("abbreviation") or stype.get("name", "")
                translated = translate_round(str(desc)) if desc else ""
                if translated and translated.lower() not in ("regular season", "temporada regular", "reg"):
                    return translated
        return ""
    except Exception:
        return ""

def get_today_utc4() -> str:
    return datetime.now(VENEZUELA_TZ).strftime("%Y-%m-%d")

def espn_date(date_str: str) -> str:
    return date_str.replace("-", "")

def parse_event_time(utc_str: str):
    try:
        dt_utc = datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
        dt_local = dt_utc.astimezone(VENEZUELA_TZ)
        return dt_local.strftime("%H:%M"), dt_local
    except Exception:
        return "--:--", None

def fetch_league(slug: str, date_str: str) -> list:
    try:
        resp = requests.get(
            f"{ESPN_BASE}/{slug}/scoreboard",
            params={"dates": espn_date(date_str), "limit": 100},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        if resp.status_code != 200:
            return []
        return resp.json().get("events", [])
    except Exception as e:
        logger.error(f"Error fetching ESPN {slug}: {e}")
        return []

def fetch_matches_for_date(local_date: str) -> dict:
    all_matches: dict = {}
    for slug, (flag, name) in LEAGUES.items():
        events = fetch_league(slug, local_date)
        for event in events:
            utc_str = event.get("date", "")
            time_str, dt_local = parse_event_time(utc_str)
            if dt_local and dt_local.strftime("%Y-%m-%d") != local_date:
                continue
            round_name = get_round_name(event)
            competitions = event.get("competitions", [{}])
            comp = competitions[0] if competitions else {}
            competitors = comp.get("competitors", [])
            home = next(
                (c.get("team", {}).get("displayName", "?")
                 for c in competitors if c.get("homeAway") == "home"), "?"
            )
            away = next(
                (c.get("team", {}).get("displayName", "?")
                 for c in competitors if c.get("homeAway") == "away"), "?"
            )
            if slug not in all_matches:
                all_matches[slug] = (flag, name, round_name, [])
            all_matches[slug][3].append({
                "home": home,
                "away": away,
                "time_str": time_str,
                "dt_local": dt_local,
            })
    return all_matches

def fetch_matches() -> dict:
    return fetch_matches_for_date(get_today_utc4())

def format_matches_message(all_matches: dict) -> str:
    header = "<b>🍿 ¡PARTIDOS DE HOY! ⚽️</b>"
    footer = "<i>⚽️ Suscríbete en t.me/iUniversoFootball</i>"
    if not all_matches:
        return header + "\n\nNo hay partidos hoy en las ligas seleccionadas.\n\n" + footer
    lines = [header]
    for slug, (flag, name, round_name, matches) in all_matches.items():
        matches_sorted = sorted(
            matches,
            key=lambda m: m["dt_local"] if m["dt_local"] else datetime.max.replace(tzinfo=VENEZUELA_TZ)
        )
        groups: dict = {}
        for match in matches_sorted:
            groups.setdefault(match["time_str"], []).append(match)
        league_header = f"<b>{flag} | {name}"
        if round_name:
            league_header += f" - {round_name}"
        league_header += "</b>"
        lines.append("")
        lines.append(league_header)
        lines.append("")
        for time_str in list(groups.keys()):
            for match in groups[time_str]:
                lines.append(f"{match['home']} - {match['away']} {time_str}")
    lines.append("")
    lines.append(footer)
    return "\n".join(lines)

async def send_matches_to_channel(bot, channel_id: str, text: str) -> None:
    if len(text) <= 1024:
        await bot.send_animation(
            chat_id=channel_id,
            animation=GIF_URL,
            caption=text,
            parse_mode="HTML",
        )
    else:
        await bot.send_animation(chat_id=channel_id, animation=GIF_URL)
        await bot.send_message(chat_id=channel_id, text=text, parse_mode="HTML")

# ─── Scheduler: partidos diarios 00:00 Venezuela ────────────────────────────
async def send_daily_matches(bot) -> None:
    if not CHANNEL_ID:
        logger.warning("No hay CHANNEL_ID configurado para partidos.")
        return
    all_matches = fetch_matches()
    msg = format_matches_message(all_matches)
    try:
        await send_matches_to_channel(bot, CHANNEL_ID, msg)
        logger.info(f"Partidos diarios enviados a {CHANNEL_ID}")
    except Exception as e:
        logger.error(f"Error al enviar partidos diarios: {e}")

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
    instancias = [
        f"https://nitter.net/{user}/rss",
        f"https://xcancel.com/{user}/rss",
        f"https://nitter.cz/{user}/rss"
    ]
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
    semilla = f"{n['texto']}{n['url']}".encode()
    tid = hashlib.md5(semilla).hexdigest()[:12]
    try:
        res = supabase.table("noticias").select("estado").eq("identificador_ia", tid).execute()
        if res.data and len(res.data) > 0:
            return False
    except Exception as e:
        logger.error(f"Error consultando Supabase: {e}")
        return False

    destino = obtener_destino(n["user"])
    try:
        completion = client_groq.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": (
                    "Eres el redactor de 'Universo Football'.\n\n"
                    "ESTRUCTURA HTML:\n"
                    "🚨🌍 (el emoji ese de planeta tierra no es obligatorio, lo puedes cambiar por alguno relacionado con el club o país) | <b>Titular</b>\n\n"
                    "▫️ Hecho 1 (máx 2 líneas).\n"
                    "▫️ Hecho 2 (máx 2 líneas).\n\n"
                    "<b>ℹ️ » [Nombre]</b> (SOLO si hay fuente clara)\n\n"
                    f"📲 <b>Suscríbete en {destino['canal_texto']}</b>\n\n"
                    "REGLAS:\n"
                    "- Usa ÚNICAMENTE el emoji ▫️ para los hechos.\n"
                    "- Prohibido usar el espacio invisible de Telegram (\\xa0).\n"
                    "- Mantén la temperatura en 0.1."
                )},
                {"role": "user", "content": f"Redacta esta noticia limpia: {n['texto']}"}
            ],
            temperature=0.1
        )
        redac = completion.choices[0].message.content.strip().replace('\xa0', '').replace('\u00a0', ' ')
    except Exception as e:
        logger.error(f"Error Groq: {e}")
        return False

    img_b = None
    if n["img"]:
        try:
            r = requests.get(n["img"], timeout=10)
            if r.status_code == 200: img_b = r.content
        except: pass

    try:
        supabase.table("noticias").insert({
            "identificador_ia": tid,
            "url_origen": n["url"],
            "estado": "en_revision"
        }).execute()
        pendientes[tid] = {
            "texto": redac, "foto": img_b, "url": n["url"],
            "owner_id": destino["user_id"], "channel_id": destino["channel_id"],
        }
        await enviar_panel_control(tid, context)
        logger.info(f"✅ Noticia enviada al panel de {destino['user_id']}: {tid}")
        return True
    except Exception as e:
        logger.error(f"Error al registrar en Supabase: {e}")
        return False

async def enviar_panel_control(tid, context):
    d = pendientes[tid]
    btn = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ PUBLICAR", callback_data=f"p:{tid}"), InlineKeyboardButton("⏰ PROGRAMAR", callback_data=f"s:{tid}")],
        [InlineKeyboardButton("📝 EDITAR TEXTO", callback_data=f"e:{tid}"), InlineKeyboardButton("🖼 CAMBIAR IMG", callback_data=f"f:{tid}")],
        [InlineKeyboardButton("🗑 BORRAR", callback_data=f"d:{tid}")]
    ])
    cap = f"🆔 <code>{tid}</code>\n\n{d['texto']}"
    destinatario = d.get("owner_id", ADMIN_ID)
    try:
        if d["foto"]:
            await context.bot.send_photo(destinatario, BytesIO(d["foto"]), caption=cap, parse_mode=ParseMode.HTML, reply_markup=btn)
        else:
            await context.bot.send_message(destinatario, cap, parse_mode=ParseMode.HTML, reply_markup=btn)
    except Exception as e:
        logger.error(f"❌ No se pudo enviar el panel a {destinatario} (tid {tid}): {e}")
        # Si falla el envío a un invitado (ej: nunca le dio /start al bot), avisa al admin
        # en vez de perder la noticia en silencio.
        if destinatario != ADMIN_ID:
            try:
                await context.bot.send_message(
                    ADMIN_ID,
                    f"⚠️ No pude enviarle el panel a tu invitado (<code>{destinatario}</code>) para "
                    f"<code>{tid}</code>.\nProbablemente no le ha dado <b>/start</b> al bot todavía.\n"
                    f"Error: <code>{e}</code>",
                    parse_mode=ParseMode.HTML
                )
            except Exception:
                pass

# ─── Comandos ───────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and update.effective_user.id in USUARIOS_AUTORIZADOS:
        es_admin = update.effective_user.id == ADMIN_ID
        comandos_extra = "• /test — Enviar partidos de hoy al canal\n• /testfecha YYYY-MM-DD — Probar partidos de una fecha\n" if es_admin else ""
        await update.message.reply_text(
            "👋 <b>Universo Football Bot</b>\n\n"
            "📋 <b>Comandos:</b>\n"
            "• /start — Este mensaje\n"
            "• /estado — Estado del bot\n"
            "• /scan — Forzar búsqueda de noticias\n"
            f"{comandos_extra}"
            "• /clear — Eliminar tus posts pendientes\n\n"
            "<i>⚽️ Suscríbete en t.me/iUniversoFootball</i>",
            parse_mode=ParseMode.HTML
        )

async def cmd_estado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and update.effective_user.id in USUARIOS_AUTORIZADOS:
        uid = update.effective_user.id
        propios = sum(1 for d in pendientes.values() if d.get("owner_id", ADMIN_ID) == uid)
        ahora_ccs = datetime.now(VENEZUELA_TZ).strftime("%H:%M:%S")
        await update.message.reply_text(
            f"✅ <b>Online</b>\n📍 Hora CCS: {ahora_ccs}\n📦 Tus pendientes: {propios}",
            parse_mode=ParseMode.HTML
        )

async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and update.effective_user.id in USUARIOS_AUTORIZADOS:
        await update.message.reply_text("🔎 Escaneando fuentes...")
        await monitoreo_wrapper(context)

async def cmd_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and update.effective_user.id != ADMIN_ID:
        return
    if not CHANNEL_ID:
        await update.message.reply_text("❌ No hay CHANNEL_ID configurado.")
        return
    await update.message.reply_text("⏳ Buscando partidos de hoy...")
    all_matches = fetch_matches()
    msg = format_matches_message(all_matches)
    try:
        await send_matches_to_channel(context.bot, CHANNEL_ID, msg)
        await update.message.reply_text(f"✅ Enviado a <code>{CHANNEL_ID}</code>.", parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f"❌ Error: <code>{e}</code>", parse_mode=ParseMode.HTML)

async def cmd_testfecha(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and update.effective_user.id != ADMIN_ID:
        return
    if not CHANNEL_ID:
        await update.message.reply_text("❌ No hay CHANNEL_ID configurado.")
        return
    if not context.args or len(context.args) != 1:
        await update.message.reply_text("❌ Uso: <code>/testfecha YYYY-MM-DD</code>", parse_mode=ParseMode.HTML)
        return
    fecha = context.args[0]
    try:
        datetime.strptime(fecha, "%Y-%m-%d")
    except ValueError:
        await update.message.reply_text("❌ Fecha inválida. Formato: <code>YYYY-MM-DD</code>", parse_mode=ParseMode.HTML)
        return
    await update.message.reply_text(f"⏳ Buscando partidos del {fecha}...")
    all_matches = fetch_matches_for_date(fecha)
    msg = format_matches_message(all_matches)
    try:
        await send_matches_to_channel(context.bot, CHANNEL_ID, msg)
        await update.message.reply_text(
            f"✅ Enviado: {fecha} → <code>{CHANNEL_ID}</code>", parse_mode=ParseMode.HTML
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Error: <code>{e}</code>", parse_mode=ParseMode.HTML)

# ─── Callbacks & Input ──────────────────────────────────────────────────────
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not q.from_user: return
    uid = q.from_user.id
    if uid not in USUARIOS_AUTORIZADOS: return
    if q.data is None or ":" not in q.data: return
    act, tid = q.data.split(":")
    if tid not in pendientes: return
    # Solo el dueño de esta noticia (admin o el invitado asignado a su cuenta) puede tocarla.
    if pendientes[tid].get("owner_id", ADMIN_ID) != uid: return
    await q.answer()
    if act == "p":
        await publicar_ahora(tid, context)
    elif act == "s":
        esperando_hora[uid] = tid
        await context.bot.send_message(uid, "⏰ Hora Caracas (24h, ej: 15:30):")
    elif act == "e":
        esperando_edicion[uid] = tid
        await context.bot.send_message(uid, "📝 Envía el nuevo texto:")
    elif act == "f":
        esperando_foto[uid] = tid
        await context.bot.send_message(uid, "📸 Envía la nueva foto:")
    elif act == "d":
        if tid in pendientes: del pendientes[tid]
        await q.delete_message()

async def recibir_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or update.effective_user.id not in USUARIOS_AUTORIZADOS: return
    uid = update.effective_user.id
    if uid in esperando_hora:
        tid = esperando_hora.pop(uid)
        try:
            h, m = map(int, update.message.text.strip().split(":"))
            ahora = datetime.now(VENEZUELA_TZ)
            prog = ahora.replace(hour=h, minute=m, second=0, microsecond=0)
            if prog <= ahora: prog += timedelta(days=1)

            async def job_publicar(ctx: ContextTypes.DEFAULT_TYPE):
                await publicar_ahora(tid, ctx)

            context.job_queue.run_once(job_publicar, when=prog.astimezone(pytz.UTC), name=tid)
            await update.message.reply_text(
                f"⏰ Programado para las <b>{update.message.text.strip()}</b> (hora Caracas)",
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logger.error(f"Error programando: {e}")
            await update.message.reply_text("❌ Formato inválido. Usa HH:MM")
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
    if not d:
        logger.warning(f"publicar_ahora: tid {tid} no en pendientes")
        return
    destino_channel = d.get("channel_id", CHANNEL_ID)
    destino_owner   = d.get("owner_id", ADMIN_ID)
    try:
        if d["foto"]:
            await context.bot.send_photo(
                destino_channel, BytesIO(d["foto"]),
                caption=d["texto"], parse_mode=ParseMode.HTML
            )
        else:
            await context.bot.send_message(
                destino_channel, d["texto"], parse_mode=ParseMode.HTML
            )
        supabase.table("noticias").update({"estado": "publicado"}).eq("identificador_ia", tid).execute()
        del pendientes[tid]
        await context.bot.send_message(
            destino_owner, f"✅ Publicado: <code>{tid}</code>", parse_mode=ParseMode.HTML
        )
        logger.info(f"Publicado: {tid} -> {destino_channel}")
    except Exception as e:
        logger.error(f"Error publicando {tid}: {e}")
        await context.bot.send_message(
            destino_owner, f"❌ Error al publicar <code>{tid}</code>:\n<code>{e}</code>",
            parse_mode=ParseMode.HTML
        )

async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or update.effective_user.id not in USUARIOS_AUTORIZADOS: return
    uid = update.effective_user.id
    tids_propios = [tid for tid, d in pendientes.items() if d.get("owner_id", ADMIN_ID) == uid]
    if not tids_propios:
        await update.message.reply_text("📭 No hay posts pendientes.")
        return
    for tid in tids_propios:
        del pendientes[tid]
    await update.message.reply_text(
        f"🗑 Se eliminaron <b>{len(tids_propios)}</b> post(s) pendiente(s).",
        parse_mode=ParseMode.HTML
    )

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

# ─── post_init: arrancar scheduler de partidos ──────────────────────────────
async def post_init(application) -> None:
    scheduler = AsyncIOScheduler(timezone=VENEZUELA_TZ)
    scheduler.add_job(
        send_daily_matches,
        trigger="cron",
        hour=0,
        minute=0,
        kwargs={"bot": application.bot}
    )
    scheduler.start()
    logger.info("Scheduler de partidos iniciado — 00:00 UTC-4.")

# ─── Main ────────────────────────────────────────────────────────────────────
def main():
    threading.Thread(target=run_http_server, daemon=True).start()

    app = (
        Application.builder()
        .token(TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start",      cmd_start))
    app.add_handler(CommandHandler("estado",     cmd_estado))
    app.add_handler(CommandHandler("scan",       cmd_scan))
    app.add_handler(CommandHandler("test",       cmd_test))
    app.add_handler(CommandHandler("testfecha",  cmd_testfecha))
    app.add_handler(CommandHandler("clear",      cmd_clear))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_input))
    app.add_handler(MessageHandler(filters.PHOTO, recibir_input))

    # Escaneo automático de noticias cada 15 minutos
    app.job_queue.run_repeating(monitoreo_wrapper, interval=900, first=10)

    logger.info("Bot Iniciado...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
