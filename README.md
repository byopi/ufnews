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

#### Opcional — Invitar a otra persona con UNA cuenta específica

Si quieres que otra persona use el bot solo con una de las cuentas de `CUENTAS_X` y publique en su propio canal (sin ver ni tocar tu cola ni tu canal), agrega también:

| Variable | Valor |
|---|---|
| `FRIEND_TELEGRAM_ID` | ID numérico de Telegram del invitado (@userinfobot) |
| `FRIEND_CHANNEL_ID` | Canal del invitado, ej: `@canal_de_tu_amigo` |
| `FRIEND_CUENTA_X` | Nombre exacto de la cuenta de X que le pertenece (debe estar en `CUENTAS_X`, ej: `Mercado_Ingles`) |
| `FRIEND_CANAL_TEXTO` | Texto de "Suscríbete en..." que aparecerá en sus posts (default: `t.me/PremierLeagueES`) |

Con esto configurado:
- Las noticias detectadas en `FRIEND_CUENTA_X` se envían **solo** al invitado, con los mismos botones (✅ Publicar / ⏰ Programar / 📝 Editar / 🖼 Cambiar imagen / 🗑 Borrar), y se publican en `FRIEND_CHANNEL_ID`.
- El resto de las cuentas siguen funcionando exactamente igual para ti (admin), publicando en `TELEGRAM_CHANNEL_ID`.
- `/estado` y `/clear` muestran/afectan solo los pendientes de quien ejecuta el comando.
- Si más adelante quieres agregar un segundo invitado con otra cuenta, se puede extender el diccionario `SUB_USUARIOS` en `bot.py`.

### Paso 4 — UptimeRobot (mantener 24/7)

1. Ve a [uptimerobot.com](https://uptimerobot.com)
2. **New Monitor**:
   - Tipo: `HTTP(s)`
   - URL: `*(tu URL de Render)*`
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
