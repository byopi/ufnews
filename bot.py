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
import random
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

# ─── Configuración Básica ──────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s]: %(message)s")
logger = logging.getLogger("universo_football")
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

pendientes: dict[str, dict] = {}
CUENTAS_X = ["mercatosphera", "Mercado_Ingles", "SoyCalcio_", "postunited"]

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
]

def get_headers():
    return {"User-Agent": random.choice(USER_AGENTS), "Accept-Language": "es-ES,es;q=0.9"}

# ─── Scraping ──────────────────────────────────────────────────────────────────
NITTER_INSTANCES = ["https://nitter.privacydev.net", "https://nitter.poast.org", "https://nitter.perennialte.ch"]

def fetch_tweets(username):
    instance = random.choice(NITTER_INSTANCES)
    try:
        resp = requests.get(f"{instance}/{username}", headers=get_headers(), timeout=15)
        if resp.status_code != 200: return []
        soup = BeautifulSoup(resp.text, "html.parser")
        tweets = []
        for item in soup.select(".timeline-item")[:3]:
            content = item.select_one(".tweet-content")
            if not content: continue
            link = item.select_one(".tweet-link")
            img = item.select_one(".attachment img")
            tweets.append({
                "texto": content.get_text(strip=True),
                "url": f"https://x.com{link['href']}" if link else f"{instance}/{username}",
                "img_url": f"{instance}{img['src']}" if img else None,
                "cuenta": username
            })
        return tweets
    except: return []

# ─── Lógica de IA y DB ─────────────────────────────────────────────────────────
async def procesar_tweet(tweet, app):
    txt = tweet["texto"]
    tweet_id = hashlib.md5(txt.encode()).hexdigest()[:12]
    
    # Evitar duplicados (Supabase)
    if len(supabase.table("noticias").select("id").eq("identificador_ia", tweet_id).execute().data) > 0: return

    tipo = gemini_model.generate_content(f"Responde solo 'fichaje' o 'noticia': {txt[:200]}").text.strip().lower()
    prompt = f"Redacta para Telegram (Markdown) esta {tipo} de futbol: {txt}. Fuente: @{tweet['cuenta']}"
    redaccion = gemini_model.generate_content(prompt).text.strip()

    supabase.table("noticias").insert({"identificador_ia": tweet_id, "url_origen": tweet["url"], "tipo": tipo, "estado": "pendiente", "texto_final": redaccion}).execute()
    
    img_data = requests.get(tweet["img_url"], headers=get_headers()).content if tweet["img_url"] else None
    pendientes[tweet_id] = {"texto": redaccion, "foto": img_data, "url": tweet["url"]}
    await enviar_previa(app, tweet_id)

async def enviar_previa(app, tid):
    data = pendientes[tid]
    btn = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Publicar", callback_data=f"p:{tid}"), InlineKeyboardButton("🗑 Borrar", callback_data=f"d:{tid}")]])
    cap = f"🆔 `{tid}`\n\n{data['texto']}"
    if data["foto"]: await app.bot.send_photo(ADMIN_ID, BytesIO(data["foto"]), caption=cap[:1024], parse_mode="Markdown", reply_markup=btn)
    else: await app.bot.send_message(ADMIN_ID, text=cap, parse_mode="Markdown", reply_markup=btn)

# ─── Comandos de Admin ────────────────────────────────────────────────────────
async def cmd_estado(update: Update, context):
    if update.effective_user.id != ADMIN_ID: return
    msg = f"📊 *Estado Universo Football*\n• Pendientes: `{len(pendientes)}`\n• Hora VE: `{datetime.now(VE_TZ).strftime('%H:%M')}`\n• Status: Online ✅"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_pendientes(update: Update, context):
    if update.effective_user.id != ADMIN_ID: return
    if not pendientes: return await update.message.reply_text("No hay noticias en espera.")
    lista = "\n".join([f"• `{k}`" for k in pendientes.keys()])
    await update.message.reply_text(f"📋 *IDs Pendientes:*\n{lista}", parse_mode="Markdown")

async def cmd_scan(update: Update, context):
    if update.effective_user.id != ADMIN_ID: return
    await update.message.reply_text("🔍 Escaneando cuentas ahora...")
    await tarea_monitoreo(context.application)

async def tarea_monitoreo(app):
    for c in CUENTAS_X:
        for t in fetch_tweets(c):
            await procesar_tweet(t, app)
            await asyncio.sleep(random.randint(5, 10))
        await asyncio.sleep(10)

async def query_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    act, tid = query.data.split(":")
    if tid not in pendientes: return
    
    if act == "p":
        d = pendientes[tid]
        if d["foto"]: await context.bot.send_photo(CHANNEL_ID, BytesIO(d["foto"]), caption=d["texto"][:1024], parse_mode="Markdown")
        else: await context.bot.send_message(CHANNEL_ID, d["texto"], parse_mode="Markdown")
        supabase.table("noticias").update({"estado": "publicado"}).eq("identificador_ia", tid).execute()
    
    del pendientes[tid]
    await query.edit_message_reply_markup(None)

# ─── Main ──────────────────────────────────────────────────────────────────────
async def post_init(app):
    sch = AsyncIOScheduler(timezone=VE_TZ)
    sch.add_job(tarea_monitoreo, "interval", minutes=15, args=[app])
    sch.start()
    await app.bot.send_message(ADMIN_ID, "🚀 Bot Reiniciado y Escaneando...")

def main():
    # Keepalive para Render
    threading.Thread(target=lambda: HTTPServer(("0.0.0.0", int(os.environ.get("PORT", 8080))), type('H', (BaseHTTPRequestHandler,), {'do_GET': lambda s: (s.send_response(200), s.end_headers(), s.wfile.write(b"OK"))})).serve_forever(), daemon=True).start()
    
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("estado", cmd_estado))
    app.add_handler(CommandHandler("scan", cmd_scan))
    app.add_handler(CommandHandler("pendientes", cmd_pendientes))
    app.add_handler(CallbackQueryHandler(query_handler))
    app.run_polling()

if __name__ == "__main__": main()
