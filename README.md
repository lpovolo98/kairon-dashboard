# Dashboard Comercial — Odoo + FastAPI

Dashboard web con 3 módulos conectado en tiempo real a Odoo Cloud.

## Módulos

| Módulo | Qué muestra |
|---|---|
| **Stock** | Stock actual · Promedio de ventas diario · Días de inventario · Semáforo rojo/amarillo/verde |
| **Ventas** | Clientes compradores · Cajas vendidas · Recompra · Gráficos por mes · Filtro por categoría |
| **Clientes** | Performance 90 días · Categorías que compran · Oportunidades de venta |
| **Agente de ventas** | Clientes de Odoo agrupados por día de visita · mapa geolocalizado · selección manual de a quién contactar · exportable con links de WhatsApp |

### Agente de ventas (`/agente`)

Primera etapa del agente que contacta clientes: **no es autónomo**, arma la lista y
deja los links listos para escribir uno por uno.

1. **Campo del día de visita.** En Kairon es el many2many de Studio
   `x_studio_many2many_field_4bq_1j6ma1s65` (un cliente puede tener más de un día),
   ya configurado por defecto y pisable con la variable de entorno `CAMPO_DIA_VISITA`.
   Si alguna vez cambia, la pantalla lo deja re-elegir: Odoo no trae un campo estándar para esto,
   así que la pantalla lista los campos de `res.partner` que suenan a día / visita / ruta
   (incluidos los `x_studio_*` de Studio y las etiquetas de contacto `category_id`),
   con los valores que hoy tienen cargados los clientes y cuántos hay en cada uno.
   Se elige uno y queda guardado en `agente_config.json` (volumen `/data`).
2. **Filtrar, ver en el mapa y marcar.** Chips por día de visita, buscador, y filtros por
   "sin teléfono", "teléfono a revisar", "sin ubicación" y "sin día". La selección se guarda
   sola en el servidor. Los que no tienen `partner_latitude` / `partner_longitude` en Odoo se
   pueden geolocalizar por dirección contra OpenStreetMap (botón *Geolocalizar faltantes*).
3. **Exportar.** Genera un HTML (o CSV) con un renglón por cliente marcado, agrupado por día,
   con el botón que abre WhatsApp con el mensaje ya escrito. Variables del mensaje:
   `{contacto}`, `{primer_nombre_contacto}`, `{nombre}` (razón social), `{dia}`.

La **persona de contacto** (el dueño o encargado, no la razón social) sale de un campo custom de
`res.partner`. Como los campos de Studio tienen nombre técnico ilegible, se detecta solo buscando
el campo cuya etiqueta es "Persona de contacto"; se puede fijar con la variable de entorno
`CAMPO_CONTACTO` o elegir a mano desde el paso 1. Si un cliente no la tiene cargada, `{contacto}`
cae en el nombre del comercio para que el mensaje nunca salga con un hueco.

Los KPIs y el panel de datos faltantes (sin teléfono, sin persona de contacto, sin ubicación,
sin día) **solo aparecen mientras haya algo que completar**: cuando la base de Odoo esté al día
desaparecen solos.

Los teléfonos se normalizan a formato WhatsApp argentino (`549` + área + número, sacando
el `0` de larga distancia y el `15`); los que quedan con largo raro se marcan *a revisar*
en vez de descartarse.

---

## Setup local (5 minutos)

### 1. Clonar e instalar dependencias

```bash
pip install -r requirements.txt
```

### 2. Crear archivo `.env`

Copiar `.env.example` como `.env` y completar con tus datos:

```bash
cp .env.example .env
```

Editar `.env`:
```
ODOO_URL=https://tuempresa.odoo.com
ODOO_DB=nombre_base_odoo
ODOO_USER=tu@email.com
ODOO_PASSWORD=tu_contraseña
CACHE_TTL_SECONDS=900
```

> **Tip**: En Odoo Cloud, el nombre de la base de datos es la parte de la URL antes de `.odoo.com`.
> Ej: si tu URL es `https://miempresa.odoo.com`, el DB es `miempresa`.

### 3. Correr el servidor

```bash
uvicorn main:app --reload --port 8000
```

Abrir `http://localhost:8000` en el browser.

---

## Deploy en Railway (acceso desde cualquier dispositivo)

Railway permite hostear gratis (~500 hs/mes) o por $5/mes ilimitado.

### Pasos:

1. Crear cuenta en [railway.app](https://railway.app)
2. Instalar Railway CLI:
   ```bash
   npm install -g @railway/cli
   ```
3. En la carpeta del proyecto:
   ```bash
   railway login
   railway init
   railway up
   ```
4. Configurar variables de entorno en Railway:
   - Ir a tu proyecto → **Variables**
   - Agregar: `ODOO_URL`, `ODOO_DB`, `ODOO_USER`, `ODOO_PASSWORD`, `CACHE_TTL_SECONDS`

5. Railway te da una URL pública tipo `https://tu-app.railway.app`

---

## Deploy en Render (alternativa gratuita)

1. Crear cuenta en [render.com](https://render.com)
2. New → Web Service → conectar repo de GitHub
3. Build command: `pip install -r requirements.txt`
4. Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
5. Agregar variables de entorno en el panel

---

## Semáforo de stock

| Color | Condición | Significado |
|---|---|---|
| 🔴 Rojo | < 7 días de inventario | Pedir urgente |
| 🟡 Amarillo | 7–21 días | Revisar pronto |
| 🟢 Verde | > 21 días | OK |

El promedio usa los **últimos 60 días** de ventas confirmadas.

---

## API endpoints

| Endpoint | Descripción |
|---|---|
| `GET /` | Dashboard web |
| `GET /api/stock` | Datos de stock + semáforo |
| `GET /api/ventas` | Informe de ventas por mes |
| `GET /api/clientes` | Listado de clientes con análisis |
| `GET /api/refresh` | Forzar recarga del caché |
| `GET /api/status` | Estado del servidor y caché |
| `GET /agente` | Agente de ventas (WhatsApp) |
| `GET /api/agente/campos` | Campos de `res.partner` candidatos a "día de visita" + valores cargados |
| `POST /api/agente/config` | Guardar el campo elegido y el mensaje de WhatsApp |
| `GET /api/agente/clientes` | Clientes con día de visita, teléfono normalizado y coordenadas |
| `POST /api/agente/seleccion` | Guardar los clientes marcados para la campaña |
| `POST /api/agente/geocodificar` | Buscar coordenadas en OpenStreetMap para los que no las tienen |
| `GET /api/agente/export` | Exportable `?formato=html\|csv` con los links wa.me |

Todos los endpoints aceptan `?force=true` para ignorar el caché.

---

## Permisos requeridos en Odoo

El usuario de Odoo necesita acceso de **lectura** a:
- `stock.quant` (inventario)
- `sale.order` + `sale.order.line` (ventas)
- `res.partner` (clientes)
- `account.move` (facturas)
- `product.product` + `product.category` (productos)
