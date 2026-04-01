# ⚽ Universo Football Bot

Bot de Telegram que monitorea cuentas de X/Twitter, procesa noticias de fútbol con **Gemini 1.5 Flash** y las publica en el canal **@iUniversoFootball** con aprobación del administrador.

---

## 🏗 Arquitectura

```
X/Twitter (Nitter) → Gemini 1.5 Flash → Supabase → Admin Telegram → Canal
                                           ↑
                                      APScheduler (cada 12 min)
```

---

## 🚀 Despliegue en Render

### Paso 1 — Supabase: Crear la tabla

En tu proyecto Supabase, ejecuta en el **SQL Editor**:

```sql
CREATE TABLE noticias (
  id              BIGSERIAL PRIMARY KEY,
  identificador_ia TEXT UNIQUE NOT NULL,
  url_origen      TEXT,
  tipo            TEXT CHECK (tipo IN ('fichaje', 'noticia')),
  estado          TEXT DEFAULT 'pendiente',
  texto_final     TEXT,
  created_at      TIMESTAMPTZ DEFAULT NOW()
);
```

### Paso 2 — GitHub

1. Crea un repositorio en GitHub
2. Sube los archivos: `bot.py`, `requirements.txt`, `render.yaml`, `.gitignore`

### Paso 3 — Render

1. Ve a [render.com](https://render.com) → **New Web Service**
2. Conecta tu repositorio de GitHub
3. Configuración:
   - **Runtime:** Python 3
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `python bot.py`
4. En **Environment Variables**, agrega:

| Variable | Valor |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Token de @BotFather |
| `ADMIN_TELEGRAM_ID` | Tu ID numérico (consúltalo en @userinfobot) |
| `TELEGRAM_CHANNEL_ID` | `@iUniversoFootball` |
| `SUPABASE_URL` | `https://XXXXXXXX.supabase.co` |
| `SUPABASE_KEY` | La `service_role` key (NO la anon key) |
| `GEMINI_API_KEY` | Tu key de aistudio.google.com |

### Paso 4 — UptimeRobot (mantener 24/7)

Igual que tu bot BCV:

1. Ve a [uptimerobot.com](https://uptimerobot.com)
2. **New Monitor**:
   - Tipo: `HTTP(s)`
   - URL: `https://universo-football-bot.onrender.com` *(tu URL de Render)*
   - Intervalo: `5 minutos`
3. Guarda — el bot nunca dormirá.

---

## 📋 Comandos del Admin

| Comando | Función |
|---|---|
| `/estado` | Estado del bot y hora Venezuela |
| `/pendientes` | Lista noticias esperando aprobación |
| `/scan` | Fuerza un escaneo inmediato |

## 🔘 Botones en las previas

| Botón | Acción |
|---|---|
| ✅ Publicar | Envía al canal @iUniversoFootball |
| 🗑 Eliminar | Descarta la noticia |
| ⏰ Programar | Pide hora HH:MM y publica automáticamente |
| 🖼 Cambiar imagen | Permite enviar una foto nueva |

---

## 📦 Dependencias principales

```
python-telegram-bot==21.6
google-generativeai==0.7.2
supabase==2.7.4
APScheduler==3.10.4
beautifulsoup4==4.12.3
requests==2.32.3
pytz==2024.1
```
