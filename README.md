# Dashboard Comercial — Odoo + FastAPI

Dashboard web con 3 módulos conectado en tiempo real a Odoo Cloud.

## Módulos

| Módulo | Qué muestra |
|---|---|
| **Stock** | Stock actual · Promedio de ventas diario · Días de inventario · Semáforo rojo/amarillo/verde |
| **Ventas** | Clientes compradores · Cajas vendidas · Recompra · Gráficos por mes · Filtro por categoría |
| **Clientes** | Performance 90 días · Categorías que compran · Oportunidades de venta |

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

Todos los endpoints aceptan `?force=true` para ignorar el caché.

---

## Permisos requeridos en Odoo

El usuario de Odoo necesita acceso de **lectura** a:
- `stock.quant` (inventario)
- `sale.order` + `sale.order.line` (ventas)
- `res.partner` (clientes)
- `account.move` (facturas)
- `product.product` + `product.category` (productos)
