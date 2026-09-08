import xmlrpc.client
import os
import time
import urllib.request
import urllib.parse
import re
import json
from datetime import datetime, date, timedelta
from collections import defaultdict
from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from pydantic import BaseModel
from PIL import Image, ImageDraw, ImageFont
from twilio.rest import Client as TwilioClient
import threading
import unicodedata

load_dotenv()

app = FastAPI(title="Odoo Dashboard API")
from administracion.api import router as administracion_router
app.include_router(administracion_router)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ─── Odoo config ────────────────────────────────────────────
ODOO_URL  = os.getenv("ODOO_URL", "")
ODOO_DB   = os.getenv("ODOO_DB", "")
ODOO_USER = os.getenv("ODOO_USER", "")
ODOO_PASS = os.getenv("ODOO_PASSWORD", "")
CACHE_TTL = int(os.getenv("CACHE_TTL_SECONDS", "900"))  # 15 min default

# ─── Twilio (reporte diario por WhatsApp) ────────────────────
TWILIO_ACCOUNT_SID   = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN    = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_WHATSAPP_FROM = os.getenv("TWILIO_WHATSAPP_FROM", "")   # ej: "whatsapp:+14155238886"
REPORTE_WHATSAPP_TO  = os.getenv("REPORTE_WHATSAPP_TO", "")    # ej: "whatsapp:+5491132308807"
REPORTE_CRON_SECRET  = os.getenv("REPORTE_CRON_SECRET", "")    # token simple para proteger el endpoint del cron

# ─── Cloudflare Access ──────────────────────────────────────
# Cloudflare pone un JWT firmado en cada request que pasa por el dominio.
# La URL de Railway no pasa por Cloudflare, asi que no lo trae: verificar la
# firma es lo que cierra esa puerta. Chequear solo que el header exista no
# alcanza — cualquiera puede mandarlo a mano contra la URL de Railway.
# El team domain no es un secreto: aparece en la URL de la pantalla de login
# de Cloudflare, que ve cualquiera. Va como default para no depender de una
# variable de entorno. El AUD sigue vacio, y sin el no se bloquea nada.
CF_ACCESS_TEAM_DOMAIN = os.getenv(
    "CF_ACCESS_TEAM_DOMAIN", "fancy-smoke-993b.cloudflareaccess.com").strip()
# Audiencia de la aplicacion de Access. Tampoco es un secreto: viaja en el
# campo "aud" de cada token que Cloudflare firma. Con esta y el team domain
# puestos, el middleware empieza a exigir.
# Para desactivarlo sin tocar codigo: poner CF_ACCESS_AUD vacia en Railway.
CF_ACCESS_AUD         = os.getenv(
    "CF_ACCESS_AUD",
    "cb9a82abc8573496c3c12b51aa83f0db89ff92309955213d510be20ba12b096b").strip()

if CF_ACCESS_TEAM_DOMAIN:
    CF_ACCESS_TEAM_DOMAIN = (CF_ACCESS_TEAM_DOMAIN
                             .replace("https://", "").replace("http://", "").rstrip("/"))

# Rutas que tienen que seguir siendo publicas:
#  - /reporte/{vendedor}: el link que se manda por WhatsApp. Los vendedores
#    lo abren sin cuenta de Access. Es de solo lectura y de un vendedor.
#  - /api/reporte-diario: lo dispara un cron externo, que se autentica con
#    su propio secret (REPORTE_CRON_SECRET).
#  - /api/status: healthcheck.
ACCESS_RUTAS_PUBLICAS   = {"/api/status", "/api/reporte-diario"}
ACCESS_PREFIJOS_PUBLICOS = ("/reporte/",)

_jwks_client = None
_jwks_lock = threading.Lock()


def access_configurado():
    return bool(CF_ACCESS_TEAM_DOMAIN and CF_ACCESS_AUD)


def _get_jwks_client():
    """Cliente de claves publicas de Cloudflare. PyJWKClient cachea las
    claves, asi que no sale a la red en cada request."""
    global _jwks_client
    with _jwks_lock:
        if _jwks_client is None:
            import jwt as _jwt
            _jwks_client = _jwt.PyJWKClient(
                f"https://{CF_ACCESS_TEAM_DOMAIN}/cdn-cgi/access/certs",
                cache_keys=True,
            )
        return _jwks_client


def verificar_token_access(token):
    """Devuelve la identidad si el JWT es valido, o None. Nunca levanta:
    cualquier problema de firma, audiencia, vencimiento o red se traduce en
    'no autenticado'."""
    if not token:
        return None
    try:
        import jwt as _jwt
        clave = _get_jwks_client().get_signing_key_from_jwt(token)
        datos = _jwt.decode(
            token, clave.key, algorithms=["RS256"],
            audience=CF_ACCESS_AUD,
            issuer=f"https://{CF_ACCESS_TEAM_DOMAIN}",
        )
        return {"email": datos.get("email", ""), "sub": datos.get("sub", "")}
    except Exception:
        return None


def _es_publica(path):
    return path in ACCESS_RUTAS_PUBLICAS or path.startswith(ACCESS_PREFIJOS_PUBLICOS)


@app.middleware("http")
async def middleware_access(request: Request, call_next):
    request.state.usuario = None
    # Si no esta configurado no se bloquea nada. Es a proposito: si el deploy
    # empezara a rechazar todo antes de que existan las variables de entorno,
    # el dashboard quedaria inaccesible incluso por el dominio.
    if not access_configurado() or _es_publica(request.url.path):
        return await call_next(request)

    token = (request.headers.get("cf-access-jwt-assertion")
             or request.cookies.get("CF_Authorization"))
    identidad = verificar_token_access(token)
    if not identidad:
        return JSONResponse(
            status_code=403,
            content={"detail": "Acceso no autorizado. Entrá por app.kaironsrl.com.ar."},
        )
    request.state.usuario = identidad
    return await call_next(request)


@app.get("/api/access/aud")
def api_access_aud(request: Request):
    """Ayuda de configuracion: dice que audiencia esta poniendo Cloudflare en
    los tokens, para poder fijar CF_ACCESS_AUD sin buscarla a mano en el
    panel de Zero Trust.

    Lee el token sin verificar la firma, porque justamente todavia no se sabe
    contra que audiencia verificar. Es seguro: solo devuelve 'aud' e 'iss',
    que son identificadores publicos que viajan en cada token, nunca el token
    en si ni datos del usuario. Y se apaga sola en cuanto Access queda
    configurado.
    """
    if access_configurado():
        return {"ya_configurado": True, "aud": CF_ACCESS_AUD}
    token = (request.headers.get("cf-access-jwt-assertion")
             or request.cookies.get("CF_Authorization"))
    if not token:
        return {"error": "No llegó ningún token de Cloudflare. "
                         "Abrí esta misma dirección en app.kaironsrl.com.ar, "
                         "no en la URL de Railway."}
    try:
        import jwt as _jwt
        claims = _jwt.decode(token, options={
            "verify_signature": False, "verify_aud": False, "verify_exp": False})
        aud = claims.get("aud")
        return {"aud": aud[0] if isinstance(aud, list) else aud,
                "iss": claims.get("iss")}
    except Exception as e:
        return {"error": f"No se pudo leer el token: {e}"}


@app.get("/api/me")
def api_me(request: Request):
    """Identidad verificada del lado del servidor. A diferencia de
    /cdn-cgi/access/get-identity, que lo resuelve Cloudflare y no existe en
    local, esto responde siempre y dice si Access esta activo."""
    u = getattr(request.state, "usuario", None)
    return {"email": (u or {}).get("email", ""), "access_activo": access_configurado()}


# ─── Cache store ────────────────────────────────────────────
_cache = {}
_cache_lock = threading.Lock()

def cache_get(key):
    with _cache_lock:
        entry = _cache.get(key)
        if entry and (time.time() - entry["ts"]) < CACHE_TTL:
            return entry["data"]
    return None

def cache_set(key, data):
    with _cache_lock:
        _cache[key] = {"data": data, "ts": time.time()}

def cache_del(key):
    with _cache_lock:
        _cache.pop(key, None)

# ─── Odoo connection ────────────────────────────────────────
def odoo_connect():
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
    uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASS, {})
    if not uid:
        raise HTTPException(status_code=401, detail="Odoo authentication failed")
    models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
    return uid, models

def odoo_call(models, uid, model, method, domain, fields, limit=5000):
    return models.execute_kw(
        ODOO_DB, uid, ODOO_PASS,
        model, method,
        [domain],
        {"fields": fields, "limit": limit}
    )

# ─── Feriados Argentina (ArgentinaDatos API) ─────────────────
_feriados_cache = {}

def get_feriados_argentina(anio):
    """Trae los feriados nacionales de Argentina para un año dado.
    Cachea en memoria por el resto de la vida del proceso (los feriados
    de un año no cambian una vez publicados)."""
    if anio in _feriados_cache:
        return _feriados_cache[anio]
    try:
        url = f"https://api.argentinadatos.com/v1/feriados/{anio}"
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read().decode())
        fechas = {item["fecha"] for item in data}  # set de "YYYY-MM-DD"
        _feriados_cache[anio] = fechas
        return fechas
    except Exception:
        # Si la API externa falla, seguimos sin feriados (solo fin de semana)
        return set()

def es_dia_habil(d, feriados_set):
    """Lunes=0 ... Domingo=6. Hábil = no es sábado/domingo y no es feriado."""
    if d.weekday() >= 5:
        return False
    if d.strftime("%Y-%m-%d") in feriados_set:
        return False
    return True

def dias_habiles_transcurridos_y_restantes(hoy=None):
    """Para el mes de 'hoy' (o el mes actual si no se especifica), devuelve
    (dias_habiles_transcurridos_incluyendo_hoy, dias_habiles_restantes_excluyendo_hoy,
    dias_habiles_totales_del_mes)."""
    if hoy is None:
        hoy = date.today()
    primer_dia = hoy.replace(day=1)
    if hoy.month == 12:
        ultimo_dia = date(hoy.year, 12, 31)
    else:
        ultimo_dia = date(hoy.year, hoy.month + 1, 1) - timedelta(days=1)

    feriados = get_feriados_argentina(hoy.year)

    transcurridos = 0
    restantes = 0
    total = 0
    d = primer_dia
    while d <= ultimo_dia:
        if es_dia_habil(d, feriados):
            total += 1
            if d <= hoy:
                transcurridos += 1
            else:
                restantes += 1
        d += timedelta(days=1)

    return transcurridos, restantes, total

def get_uom_factors(models, uid):
    """Trae el factor real de TODAS las UdM del sistema (uom.uom.factor).
    Este factor ya está cargado correctamente en Odoo para cada empaque
    (Caja=16, Displays=12, Six-Pack=6, Display x10u=10, etc), así que no
    hace falta mantener una tabla manual — usamos la fuente de verdad."""
    uoms = models.execute_kw(
        ODOO_DB, uid, ODOO_PASS,
        "uom.uom", "search_read", [[]],
        {"fields": ["id", "name", "factor"]}
    )
    return {u["id"]: (u["factor"] or 1) for u in uoms}

def normalizar_qty(qty, uom_id_tuple, uom_factors):
    """qty viene expresado en la UdM elegida en esa línea específica
    (puede ser Units, Caja, Displays, Six-Pack, etc — varía línea a línea
    incluso para el mismo producto). El factor de esa UdM (ya cargado en
    Odoo) indica cuántas unidades base representa, así que normalizamos
    multiplicando por ese factor."""
    if not uom_id_tuple:
        return qty
    uom_id = uom_id_tuple[0]
    factor = uom_factors.get(uom_id, 1)
    return qty * factor

def get_proveedores_por_producto(models, uid):
    """Trae el proveedor real de cada producto desde product.supplierinfo
    (la pestaña 'Compras' de la ficha del producto en Odoo) — fuente de
    verdad en vez de mantener un mapeo manual en el frontend.
    Si un producto tiene más de un proveedor cargado (por ej. quedó un
    registro viejo sin borrar), nos quedamos con el de id más alto, que
    es el cargado más recientemente."""
    registros = models.execute_kw(ODOO_DB, uid, ODOO_PASS,
        "product.supplierinfo", "search_read",
        [[]],
        {"fields": ["id", "partner_id", "product_tmpl_id"]}
    )
    # Si hay varios registros para el mismo template, nos quedamos con el último (id más alto)
    por_tmpl = {}
    for r in registros:
        if not r.get("product_tmpl_id") or not r.get("partner_id"):
            continue
        tmpl_id = r["product_tmpl_id"][0]
        if tmpl_id not in por_tmpl or r["id"] > por_tmpl[tmpl_id]["id"]:
            por_tmpl[tmpl_id] = {"id": r["id"], "proveedor": r["partner_id"][1]}
    return {tmpl_id: v["proveedor"] for tmpl_id, v in por_tmpl.items()}

# ─── Objetivos (persistidos en Railway Volume) ───────────────
# En producción (Railway) el volumen está montado en /data, así que
# escribir ahí sobrevive a cualquier deploy nuevo. En desarrollo local,
# si /data no existe, usamos una carpeta local — no persiste entre
# máquinas pero al menos no rompe nada al correr localmente.
_DATA_DIR = "/data" if os.path.isdir("/data") else os.path.dirname(os.path.abspath(__file__))
OBJETIVOS_PATH = os.path.join(_DATA_DIR, "objetivos.json")
_objetivos_lock = threading.Lock()

def cargar_objetivos():
    """Estructura: { "2026-06": { "JK": { "Alimentos Argentinos Nutregal SA":
       {"facturacion": 3000000, "cajas": 500, "cobertura": 80}, ... }, "KAIRON": {...} } }"""
    with _objetivos_lock:
        if not os.path.exists(OBJETIVOS_PATH):
            return {}
        try:
            with open(OBJETIVOS_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

def guardar_objetivos(data):
    with _objetivos_lock:
        with open(OBJETIVOS_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

# ─── Data builders ──────────────────────────────────────────

def build_stock_data(uid, models):
    """Stock actual en CAJAS + promedio de ventas (facturado) en CAJAS + días de inventario.
    Todo el módulo trabaja en cajas: stock físico ÷ unid_caja, venta facturada normalizada ÷ unid_caja."""
    uom_factors = get_uom_factors(models, uid)
    proveedores_map = get_proveedores_por_producto(models, uid)

    # Stock actual por producto (Odoo lo guarda en unidades base del producto)
    quants = odoo_call(models, uid, "stock.quant", "search_read",
        [["location_id.usage", "=", "internal"]],
        ["product_id", "quantity", "reserved_quantity"]
    )
    stock_map = defaultdict(float)
    for q in quants:
        if q["product_id"]:
            pid = q["product_id"][0]
            stock_map[pid] += (q["quantity"] - q.get("reserved_quantity", 0))

    # Ventas últimos 2 meses (cantidad FACTURADA, normalizada a unidades reales
    # usando el factor real de la UdM de cada línea — ver normalizar_qty)
    fecha_desde = (date.today() - timedelta(days=60)).strftime("%Y-%m-%d")
    lineas = odoo_call(models, uid, "sale.order.line", "search_read",
        [["order_id.state", "in", ["sale", "done"]],
         ["order_id.date_order", ">=", fecha_desde]],
        ["product_id", "qty_invoiced", "product_uom_id", "order_id"]
    )

    venta_map = defaultdict(float)
    for l in lineas:
        if l["product_id"]:
            pid = l["product_id"][0]
            venta_map[pid] += normalizar_qty(l["qty_invoiced"], l["product_uom_id"], uom_factors)

    # Venta del último mes cerrado. Va en una consulta aparte y no se deduce
    # de los 60 días de arriba porque ese rango no siempre cubre el mes
    # anterior completo: un 31 de octubre, 60 días atrás cae el 1 de
    # septiembre y se perdería el primer día del mes.
    fin_mes_ant = date.today().replace(day=1) - timedelta(days=1)
    ini_mes_ant = fin_mes_ant.replace(day=1)
    lineas_mes_ant = odoo_call(models, uid, "sale.order.line", "search_read",
        [["order_id.state", "in", ["sale", "done"]],
         ["order_id.date_order", ">=", ini_mes_ant.strftime("%Y-%m-%d 00:00:00")],
         ["order_id.date_order", "<=", fin_mes_ant.strftime("%Y-%m-%d 23:59:59")]],
        ["product_id", "qty_invoiced", "product_uom_id"]
    )
    venta_mes_ant_map = defaultdict(float)
    for l in lineas_mes_ant:
        if l["product_id"]:
            venta_mes_ant_map[l["product_id"][0]] += normalizar_qty(
                l["qty_invoiced"], l["product_uom_id"], uom_factors)
    MES_ANT = ini_mes_ant.strftime("%Y-%m")

    # Info productos
    pids = list(set(list(stock_map.keys()) + list(venta_map.keys())
                    + list(venta_mes_ant_map.keys())))
    if not pids:
        return []
    productos = models.execute_kw(ODOO_DB, uid, ODOO_PASS,
        "product.product", "search_read",
        [[["id", "in", pids]]],
        {"fields": ["id", "name", "categ_id", "default_code", "uom_id", "standard_price",
                    "x_studio_unidades_por_caja", "product_tmpl_id"]}
    )

    dias = 60
    result = []
    for p in productos:
        pid = p["id"]
        unid_caja = p.get("x_studio_unidades_por_caja") or 1
        tmpl_id = p["product_tmpl_id"][0] if p.get("product_tmpl_id") else None
        proveedor = proveedores_map.get(tmpl_id, "Sin proveedor")

        # Stock y venta, convertidos de unidades a CAJAS
        stock_unidades = stock_map.get(pid, 0)
        venta_unidades = venta_map.get(pid, 0)
        stock_cajas = round(stock_unidades / unid_caja, 2) if unid_caja > 0 else stock_unidades
        venta_ma_unid = venta_mes_ant_map.get(pid, 0)
        venta_mes_ant = round(venta_ma_unid / unid_caja, 1) if unid_caja > 0 else round(venta_ma_unid, 1)
        avg_diario_cajas = round((venta_unidades / unid_caja) / dias, 3) if unid_caja > 0 else round(venta_unidades / dias, 3)

        dias_inv = round(stock_cajas / avg_diario_cajas, 1) if avg_diario_cajas > 0 else 9999

        # Semáforo: rojo < 7 días, amarillo < 21, verde >= 21
        if dias_inv < 7:
            semaforo = "red"
        elif dias_inv < 21:
            semaforo = "yellow"
        else:
            semaforo = "green"

        costo_unidad = round(p.get("standard_price", 0) or 0, 2)
        costo_caja = round(costo_unidad * unid_caja, 2)
        result.append({
            "id": pid,
            "codigo": p.get("default_code") or "",
            "nombre": p["name"],
            "categoria": p["categ_id"][1] if p["categ_id"] else "Sin categoría",
            "proveedor": proveedor,
            "uom": "Cajas",
            "unid_caja": unid_caja,
            "stock_actual": stock_cajas,
            "venta_mes_ant": venta_mes_ant,
            "mes_ant": MES_ANT,
            "avg_diario": avg_diario_cajas,
            "avg_mensual": round(avg_diario_cajas * 30, 1),
            "dias_inventario": dias_inv,
            "semaforo": semaforo,
            "costo": costo_caja,
            "valorizado": round(stock_cajas * costo_caja, 2),
        })

    result.sort(key=lambda x: x["dias_inventario"])
    return result


def build_ventas_data(uid, models):
    """Ventas detalladas: trae líneas con producto, proveedor, segmento, cajas, monto, cliente, mes,
    vendedor, canal y tipo de comercio del cliente.
    El frontend hace todo el filtrado/agrupado interactivo a partir de esta data cruda."""
    fecha_desde = (date.today() - timedelta(days=365)).strftime("%Y-%m-%d")

    uom_factors = get_uom_factors(models, uid)
    proveedores_map = get_proveedores_por_producto(models, uid)

    ordenes = odoo_call(models, uid, "sale.order", "search_read",
        [["state", "in", ["sale", "done"]], ["date_order", ">=", fecha_desde]],
        ["id", "partner_id", "date_order", "amount_total", "user_id"]
    )
    order_ids = [o["id"] for o in ordenes]
    order_map = {o["id"]: o for o in ordenes}

    lineas = []
    if order_ids:
        lineas = odoo_call(models, uid, "sale.order.line", "search_read",
            [["order_id", "in", order_ids]],
            ["order_id", "product_id", "qty_invoiced", "product_uom_id", "price_subtotal", "price_total"]
        )

    # Info de productos: categoría, costo, unidades por caja, proveedor real (Compras)
    prod_ids = list({l["product_id"][0] for l in lineas if l["product_id"]})
    prod_info = {}
    if prod_ids:
        prods = models.execute_kw(ODOO_DB, uid, ODOO_PASS,
            "product.product", "search_read",
            [[["id", "in", prod_ids]]],
            {"fields": ["id", "name", "categ_id", "default_code", "x_studio_unidades_por_caja", "product_tmpl_id"]}
        )
        for p in prods:
            tmpl_id = p["product_tmpl_id"][0] if p.get("product_tmpl_id") else None
            prod_info[p["id"]] = {
                "nombre":   p["name"],
                "categoria": p["categ_id"][1] if p["categ_id"] else "Sin categoría",
                "codigo":   p.get("default_code") or "",
                "unid_caja": p.get("x_studio_unidades_por_caja") or 1,
                "proveedor": proveedores_map.get(tmpl_id, "Sin proveedor"),
            }

    # Info de clientes: canal y tipo de comercio (res.partner)
    partner_ids = list({o["partner_id"][0] for o in ordenes if o["partner_id"]})
    partner_info = {}
    if partner_ids:
        partners = models.execute_kw(ODOO_DB, uid, ODOO_PASS,
            "res.partner", "search_read",
            [[["id", "in", partner_ids]]],
            {"fields": ["id", "x_studio_canal", "x_studio_tipo_de_comercio"]}
        )
        for p in partners:
            partner_info[p["id"]] = {
                "canal": p.get("x_studio_canal") or "Sin canal",
                "tipo_comercio": p.get("x_studio_tipo_de_comercio") or "Sin tipo",
            }

    # Construir filas detalladas (una por línea de venta).
    # Métrica principal: CANTIDAD FACTURADA, normalizada a unidades reales
    # usando el factor REAL de la UdM de Odoo (uom.uom.factor) — no una tabla
    # manual. Cada línea puede estar en una UdM distinta (Units, Caja,
    # Displays, Six-Pack...) según cómo la cargó el vendedor; el factor de
    # esa UdM específica indica cuántas unidades base representa.
    filas = []
    for l in lineas:
        oid = l["order_id"][0] if l["order_id"] else None
        if not oid or oid not in order_map:
            continue
        orden = order_map[oid]
        pid = l["product_id"][0] if l["product_id"] else None
        if not pid or pid not in prod_info:
            continue
        info = prod_info[pid]
        unid_caja = info["unid_caja"] or 1

        unidades_facturadas = normalizar_qty(l["qty_invoiced"], l["product_uom_id"], uom_factors)
        cajas = unidades_facturadas / unid_caja if unid_caja > 0 else 0

        pid_partner = orden["partner_id"][0] if orden["partner_id"] else None
        pinfo = partner_info.get(pid_partner, {"canal": "Sin canal", "tipo_comercio": "Sin tipo"})

        filas.append({
            "mes":            orden["date_order"][:7],
            "fecha":          orden["date_order"][:10],
            "partner_id":     pid_partner,
            "partner_nom":    orden["partner_id"][1] if orden["partner_id"] else "Sin cliente",
            "vendedor":       orden["user_id"][1] if orden.get("user_id") else "Sin vendedor",
            "canal":          pinfo["canal"],
            "tipo_comercio":  pinfo["tipo_comercio"],
            "product_id":     pid,
            "producto":       info["nombre"],
            "codigo":         info["codigo"],
            "categoria":      info["categoria"],
            "proveedor":      info["proveedor"],
            "unidades":       round(unidades_facturadas, 2),
            "cajas":          round(cajas, 3),
            "monto_total":    round(l.get("price_total", l["price_subtotal"]), 2),
        })

    return filas


def build_cartera_data(uid, models):
    """Clientes en cartera real (filtro de negocio: tiene órdenes y no es un
    contacto interno/genérico), con su vendedor, canal y tipo de comercio.
    Es la base fija para calcular cobertura de cartera por vendedor/canal."""
    domain = ["&", "&", "&",
        ["sale_order_ids", "!=", False],
        ["name", "not ilike", "NUTREGAL"],
        ["name", "not ilike", "MERCADERIA"],
        ["name", "not ilike", "Julio K"]
    ]
    partners = models.execute_kw(ODOO_DB, uid, ODOO_PASS,
        "res.partner", "search_read",
        [domain],
        {"fields": ["id", "name", "user_id", "x_studio_canal", "x_studio_tipo_de_comercio"]}
    )
    return [{
        "id": p["id"],
        "nombre": p["name"],
        "vendedor": p["user_id"][1] if p.get("user_id") else "Sin vendedor",
        "canal": p.get("x_studio_canal") or "Sin canal",
        "tipo_comercio": p.get("x_studio_tipo_de_comercio") or "Sin tipo",
    } for p in partners]


def build_clientes_data(uid, models):
    """Listado de clientes con análisis de compra y oportunidades"""
    fecha_desde = (date.today() - timedelta(days=90)).strftime("%Y-%m-%d")
    mes_actual  = date.today().strftime("%Y-%m")

    # Todos los clientes activos
    partners = odoo_call(models, uid, "res.partner", "search_read",
        [["customer_rank", ">", 0], ["active", "=", True]],
        ["id", "name", "email", "phone", "city"]
    )

    ordenes = odoo_call(models, uid, "sale.order", "search_read",
        [["state", "in", ["sale", "done"]], ["date_order", ">=", fecha_desde]],
        ["id", "partner_id", "date_order", "amount_total", "user_id"]
    )
    order_ids = [o["id"] for o in ordenes]
    order_by_partner = defaultdict(list)
    for o in ordenes:
        if o["partner_id"]:
            order_by_partner[o["partner_id"][0]].append(o)

    # Líneas para ver categorías
    lineas = []
    if order_ids:
        lineas = odoo_call(models, uid, "sale.order.line", "search_read",
            [["order_id", "in", order_ids]],
            ["order_id", "product_id", "product_qty", "price_subtotal"]
        )

    prod_ids = list({l["product_id"][0] for l in lineas if l["product_id"]})
    prod_categ = {}
    if prod_ids:
        prods = models.execute_kw(ODOO_DB, uid, ODOO_PASS,
            "product.product", "search_read",
            [[["id", "in", prod_ids]]],
            {"fields": ["id", "categ_id"]}
        )
        prod_categ = {p["id"]: (p["categ_id"][1] if p["categ_id"] else "Sin categoría") for p in prods}

    # Todas las categorías disponibles
    all_categs_raw = models.execute_kw(ODOO_DB, uid, ODOO_PASS,
        "product.category", "search_read", [[]], {"fields": ["id", "name"]}
    )
    all_categs = {c["name"] for c in all_categs_raw}

    # Líneas por orden
    lineas_by_order = defaultdict(list)
    for l in lineas:
        if l["order_id"]:
            lineas_by_order[l["order_id"][0]].append(l)

    result = []
    for p in partners:
        pid = p["id"]
        ords = order_by_partner.get(pid, [])
        compro_este_mes = any(o["date_order"][:7] == mes_actual for o in ords)
        monto_90d = sum(o["amount_total"] for o in ords)
        num_ordenes = len(ords)

        # Vendedores que le vendieron en los últimos 90 días (sale.order.user_id,
        # puede ser más de uno si distintas órdenes tuvieron distinto vendedor)
        vendedores = sorted({o["user_id"][1] for o in ords if o.get("user_id")})

        # Fecha del último pedido (cualquiera, no solo de este mes)
        ultimo_pedido_fecha = None
        if ords:
            ultimo_pedido_fecha = max(ords, key=lambda o: o["date_order"])["date_order"][:10]

        # Categorías que compró
        categs_compradas = set()
        for o in ords:
            for l in lineas_by_order.get(o["id"], []):
                if l["product_id"]:
                    c = prod_categ.get(l["product_id"][0])
                    if c:
                        categs_compradas.add(c)

        # Categorías que NO compró = oportunidad
        categs_faltantes = all_categs - categs_compradas

        # Oportunidad text
        if not ords:
            oportunidad = "Sin compras en 90 días — reactivar"
        elif not compro_este_mes:
            ultimo_pedido = max(ords, key=lambda o: o["date_order"])
            dias_desde_ultimo = (date.today() - date.fromisoformat(ultimo_pedido["date_order"][:10])).days
            oportunidad = f"No compró este mes — último pedido hace {dias_desde_ultimo} días"
        elif categs_faltantes:
            oportunidad = f"No compra: {', '.join(sorted(categs_faltantes)[:3])}"
        else:
            oportunidad = "Cliente activo en todas las categorías"

        result.append({
            "id": pid,
            "nombre": p["name"],
            "email": p.get("email") or "",
            "ciudad": p.get("city") or "",
            "compro_este_mes": compro_este_mes,
            "ordenes_90d": num_ordenes,
            "monto_90d": round(monto_90d, 2),
            "categorias_compradas": sorted(categs_compradas),
            "categorias_faltantes": sorted(categs_faltantes),
            "oportunidad": oportunidad,
            "vendedores": vendedores,
            "ultimo_pedido_fecha": ultimo_pedido_fecha,
        })

    result.sort(key=lambda x: (-x["monto_90d"]))
    return result


def build_cliente_detalle_data(uid, models, partner_id):
    """Histórico completo de ventas de UN cliente (365 días): líneas con
    producto, proveedor, SKU, mes, cajas y facturación — para el detalle
    expandido al hacer click en un cliente desde el módulo Clientes."""
    fecha_desde = (date.today() - timedelta(days=365)).strftime("%Y-%m-%d")

    uom_factors = get_uom_factors(models, uid)
    proveedores_map = get_proveedores_por_producto(models, uid)

    ordenes = odoo_call(models, uid, "sale.order", "search_read",
        [["partner_id", "=", partner_id], ["state", "in", ["sale", "done"]],
         ["date_order", ">=", fecha_desde]],
        ["id", "date_order", "amount_total", "user_id", "name"]
    )
    order_ids = [o["id"] for o in ordenes]
    order_map = {o["id"]: o for o in ordenes}

    lineas = []
    if order_ids:
        lineas = odoo_call(models, uid, "sale.order.line", "search_read",
            [["order_id", "in", order_ids]],
            ["order_id", "product_id", "qty_invoiced", "product_uom_id", "price_subtotal", "price_total"]
        )

    prod_ids = list({l["product_id"][0] for l in lineas if l["product_id"]})
    prod_info = {}
    if prod_ids:
        prods = models.execute_kw(ODOO_DB, uid, ODOO_PASS,
            "product.product", "search_read",
            [[["id", "in", prod_ids]]],
            {"fields": ["id", "name", "categ_id", "default_code", "x_studio_unidades_por_caja", "product_tmpl_id"]}
        )
        for p in prods:
            tmpl_id = p["product_tmpl_id"][0] if p.get("product_tmpl_id") else None
            prod_info[p["id"]] = {
                "nombre":   p["name"],
                "categoria": p["categ_id"][1] if p["categ_id"] else "Sin categoría",
                "codigo":   p.get("default_code") or "",
                "unid_caja": p.get("x_studio_unidades_por_caja") or 1,
                "proveedor": proveedores_map.get(tmpl_id, "Sin proveedor"),
            }

    filas = []
    for l in lineas:
        oid = l["order_id"][0] if l["order_id"] else None
        if not oid or oid not in order_map:
            continue
        orden = order_map[oid]
        pid = l["product_id"][0] if l["product_id"] else None
        if not pid or pid not in prod_info:
            continue
        info = prod_info[pid]
        unid_caja = info["unid_caja"] or 1
        unidades_facturadas = normalizar_qty(l["qty_invoiced"], l["product_uom_id"], uom_factors)
        cajas = unidades_facturadas / unid_caja if unid_caja > 0 else 0

        filas.append({
            "mes":          orden["date_order"][:7],
            "fecha":        orden["date_order"][:10],
            "orden":        orden.get("name", ""),
            "vendedor":     orden["user_id"][1] if orden.get("user_id") else "Sin vendedor",
            "producto":     info["nombre"],
            "codigo":       info["codigo"],
            "categoria":    info["categoria"],
            "proveedor":    info["proveedor"],
            "cajas":        round(cajas, 3),
            "monto_total":  round(l.get("price_total", l["price_subtotal"]), 2),
        })

    return filas





def build_cobranzas_data(uid, models):
    """Cuentas a cobrar: facturas vencidas y a vencer, clustering por aging"""
    today = date.today()

    # Facturas de cliente abiertas (pendientes de cobro)
    facturas = models.execute_kw(
        ODOO_DB, uid, ODOO_PASS,
        "account.move", "search_read",
        [[["move_type", "in", ["out_invoice", "out_refund"]],
          ["state", "=", "posted"],
          ["payment_state", "in", ["not_paid", "partial"]]]],
        {"fields": ["id", "name", "partner_id", "invoice_date", "invoice_date_due",
                    "amount_total", "amount_residual", "currency_id", "payment_state"]}
    )

    # Ventas del mes anterior (para % deuda / ventas)
    primer_dia_mes = today.replace(day=1)
    ultimo_mes_fin = primer_dia_mes - timedelta(days=1)
    ultimo_mes_ini = ultimo_mes_fin.replace(day=1)
    ventas_mes_ant = models.execute_kw(
        ODOO_DB, uid, ODOO_PASS,
        "sale.order", "search_read",
        [[["state", "in", ["sale", "done"]],
          ["date_order", ">=", ultimo_mes_ini.strftime("%Y-%m-%d")],
          ["date_order", "<=", ultimo_mes_fin.strftime("%Y-%m-%d")]]],
        {"fields": ["amount_total"]}
    )
    total_ventas_mes_ant = sum(v["amount_total"] for v in ventas_mes_ant)

    # Clustering aging
    buckets = {
        "a_vencer":    {"label": "A vencer",          "min": None, "max": 0,   "total": 0, "count": 0},
        "v_0_7":       {"label": "Vencido 0-7 días",  "min": 0,    "max": 7,   "total": 0, "count": 0},
        "v_7_14":      {"label": "Vencido 7-14 días", "min": 7,    "max": 14,  "total": 0, "count": 0},
        "v_14_30":     {"label": "Vencido 14-30 días","min": 14,   "max": 30,  "total": 0, "count": 0},
        "v_30_60":     {"label": "Vencido 30-60 días","min": 30,   "max": 60,  "total": 0, "count": 0},
        "v_mas_60":    {"label": "Vencido +60 días",  "min": 60,   "max": None,"total": 0, "count": 0},
    }

    clientes_map = defaultdict(lambda: {
        "facturas": [], "total_deuda": 0, "max_vencimiento": 0
    })

    facturas_detalle = []
    for f in facturas:
        monto = f["amount_residual"]
        if monto <= 0:
            continue
        partner_id   = f["partner_id"][0] if f["partner_id"] else None
        partner_nom  = f["partner_id"][1] if f["partner_id"] else "Sin cliente"
        fecha_venc   = date.fromisoformat(f["invoice_date_due"]) if f["invoice_date_due"] else today
        dias_venc    = (today - fecha_venc).days  # positivo = vencido, negativo = a vencer

        # Bucket
        if dias_venc <= 0:
            bucket = "a_vencer"
        elif dias_venc <= 7:
            bucket = "v_0_7"
        elif dias_venc <= 14:
            bucket = "v_7_14"
        elif dias_venc <= 30:
            bucket = "v_14_30"
        elif dias_venc <= 60:
            bucket = "v_30_60"
        else:
            bucket = "v_mas_60"

        buckets[bucket]["total"] += monto
        buckets[bucket]["count"] += 1

        clientes_map[partner_id]["total_deuda"] += monto
        clientes_map[partner_id]["nombre"] = partner_nom
        clientes_map[partner_id]["max_vencimiento"] = max(
            clientes_map[partner_id]["max_vencimiento"], dias_venc)
        clientes_map[partner_id]["facturas"].append({
            "id":          f["id"],
            "numero":      f["name"],
            "tipo":        "Factura",
            "fecha":       f.get("invoice_date") or "",
            "vencimiento": f["invoice_date_due"] or "",
            "dias_venc":   dias_venc,
            "monto_orig":  round(f["amount_total"], 2),
            "saldo":       round(monto, 2),
            "estado_pago": f["payment_state"],
            "bucket":      bucket,
        })

        facturas_detalle.append({
            "partner_id":  partner_id,
            "partner_nom": partner_nom,
            "numero":      f["name"],
            "fecha":       f.get("invoice_date") or "",
            "vencimiento": f["invoice_date_due"] or "",
            "dias_venc":   dias_venc,
            "saldo":       round(monto, 2),
            "bucket":      bucket,
        })

    total_deuda = sum(b["total"] for b in buckets.values())
    total_vencido = sum(b["total"] for k, b in buckets.items() if k != "a_vencer")

    # Obtener pagos y NC por cliente (para estado de cuenta completo)
    partner_ids = list(clientes_map.keys())
    movimientos_map = defaultdict(list)
    if partner_ids:
        # Pagos recibidos
        pagos = models.execute_kw(
            ODOO_DB, uid, ODOO_PASS,
            "account.payment", "search_read",
            [[["partner_id", "in", partner_ids],
              ["payment_type", "=", "inbound"],
              ["state", "=", "posted"]]],
            {"fields": ["id", "name", "partner_id", "date", "amount"], "limit": 5000}
        )
        for p in pagos:
            pid = p["partner_id"][0] if p["partner_id"] else None
            if pid and pid in clientes_map:
                movimientos_map[pid].append({
                    "tipo":        "Pago",
                    "numero":      p.get("name") or "Pago",
                    "fecha":       p.get("date") or "",
                    "vencimiento": "",
                    "monto_orig":  round(p["amount"], 2),
                    "saldo":       -round(p["amount"], 2),
                    "estado_pago": "posted",
                })
        # Notas de crédito aplicadas
        ncs = models.execute_kw(
            ODOO_DB, uid, ODOO_PASS,
            "account.move", "search_read",
            [[["move_type", "=", "out_refund"],
              ["state", "=", "posted"],
              ["partner_id", "in", partner_ids]]],
            {"fields": ["id", "name", "partner_id", "invoice_date", "amount_total", "amount_residual"]}
        )
        for nc in ncs:
            pid = nc["partner_id"][0] if nc["partner_id"] else None
            if pid and pid in clientes_map:
                movimientos_map[pid].append({
                    "tipo":        "Nota de Crédito",
                    "numero":      nc["name"],
                    "fecha":       nc.get("invoice_date") or "",
                    "vencimiento": "",
                    "monto_orig":  round(nc["amount_total"], 2),
                    "saldo":       -round(nc["amount_total"], 2),
                    "estado_pago": "posted",
                })

    # Top clientes ordenados por deuda
    clientes_list = []
    for pid, data in clientes_map.items():
        movs = sorted(movimientos_map.get(pid, []), key=lambda x: x["fecha"], reverse=True)
        clientes_list.append({
            "id":              pid,
            "nombre":          data["nombre"],
            "total_deuda":     round(data["total_deuda"], 2),
            "max_vencimiento": data["max_vencimiento"],
            "facturas":        sorted(data["facturas"], key=lambda x: x["dias_venc"], reverse=True),
            "movimientos":     movs,
        })
    clientes_list.sort(key=lambda x: -x["total_deuda"])

    # Días de venta en la calle
    dias_venta_calle = round(total_deuda / (total_ventas_mes_ant / 30), 1) if total_ventas_mes_ant > 0 else None
    pct_deuda_ventas = round(total_deuda / total_ventas_mes_ant * 100, 1) if total_ventas_mes_ant > 0 else None

    return {
        "total_deuda":          round(total_deuda, 2),
        "total_vencido":        round(total_vencido, 2),
        "buckets":              {k: {"label": v["label"], "total": round(v["total"],2), "count": v["count"]} for k,v in buckets.items()},
        "clientes":             clientes_list,
        "total_ventas_mes_ant": round(total_ventas_mes_ant, 2),
        "mes_anterior":         ultimo_mes_fin.strftime("%B %Y"),
        "dias_venta_calle":     dias_venta_calle,
        "pct_deuda_ventas":     pct_deuda_ventas,
        "cantidad_facturas":    len(facturas_detalle),
        "fecha_calculo":        today.isoformat(),
    }

# ─── API Routes ─────────────────────────────────────────────

@app.get("/api/stock")
def get_stock(force: bool = False):
    cached = cache_get("stock")
    if cached and not force:
        return {"data": cached, "cached": True, "ttl": CACHE_TTL}
    uid, models = odoo_connect()
    data = build_stock_data(uid, models)
    cache_set("stock", data)
    return {"data": data, "cached": False, "ttl": CACHE_TTL}

@app.get("/api/ventas")
def get_ventas(force: bool = False):
    cached = cache_get("ventas")
    if cached and not force:
        return {"data": cached, "cached": True, "ttl": CACHE_TTL}
    uid, models = odoo_connect()
    data = build_ventas_data(uid, models)
    cache_set("ventas", data)
    return {"data": data, "cached": False, "ttl": CACHE_TTL}

@app.get("/api/clientes")
def get_clientes(force: bool = False):
    cached = cache_get("clientes")
    if cached and not force:
        return {"data": cached, "cached": True, "ttl": CACHE_TTL}
    uid, models = odoo_connect()
    data = build_clientes_data(uid, models)
    cache_set("clientes", data)
    return {"data": data, "cached": False, "ttl": CACHE_TTL}

@app.get("/api/cartera")
def get_cartera(force: bool = False):
    cached = cache_get("cartera")
    if cached and not force:
        return {"data": cached, "cached": True, "ttl": CACHE_TTL}
    uid, models = odoo_connect()
    data = build_cartera_data(uid, models)
    cache_set("cartera", data)
    return {"data": data, "cached": False, "ttl": CACHE_TTL}

# Caché propio para detalle de cliente (key = partner_id), TTL más corto
# porque son muchos clientes posibles y no vale la pena guardarlos todos
# para siempre en el caché principal.
_cliente_detalle_cache = {}
CLIENTE_DETALLE_TTL = 300  # 5 minutos

@app.get("/api/cliente/{partner_id}/detalle")
def get_cliente_detalle(partner_id: int, force: bool = False):
    now = time.time()
    cached = _cliente_detalle_cache.get(partner_id)
    if cached and not force and (now - cached["ts"]) < CLIENTE_DETALLE_TTL:
        return {"data": cached["data"], "cached": True}
    uid, models = odoo_connect()
    data = build_cliente_detalle_data(uid, models, partner_id)
    _cliente_detalle_cache[partner_id] = {"data": data, "ts": now}
    return {"data": data, "cached": False}

# ─── Objetivos ────────────────────────────────────────────────
class ObjetivoProveedor(BaseModel):
    facturacion: float = 0
    cajas: float = 0
    cobertura: float = 0  # % objetivo de cobertura de cartera (0-100)

class GuardarObjetivosBody(BaseModel):
    mes: str  # "YYYY-MM"
    vendedor: str  # "JK" o "KAIRON"
    objetivos: dict[str, ObjetivoProveedor]  # { "Proveedor X": {...}, ... }

@app.get("/api/objetivos")
def get_objetivos(mes: str = None):
    """Si se pasa ?mes=YYYY-MM devuelve solo ese mes, sino todo el histórico."""
    data = cargar_objetivos()
    if mes:
        return {"mes": mes, "objetivos": data.get(mes, {})}
    return {"objetivos": data}

@app.post("/api/objetivos")
def post_objetivos(body: GuardarObjetivosBody):
    data = cargar_objetivos()
    if body.mes not in data:
        data[body.mes] = {}
    data[body.mes][body.vendedor] = {k: v.dict() for k, v in body.objetivos.items()}
    guardar_objetivos(data)
    return {"ok": True}

def build_objetivos_avance(uid, models, mes):
    """Cruza los objetivos guardados de un mes con la venta real (Ventas +
    Cartera) para armar la matriz Vendedor > Proveedor con % de cumplimiento.
    Reutilizada por el endpoint HTTP y por el reporte diario de WhatsApp."""
    objetivos_data = cargar_objetivos().get(mes, {})

    ventas = build_ventas_data(uid, models)
    cartera = build_cartera_data(uid, models)

    ventas_mes = [v for v in ventas if v["mes"] == mes]

    cartera_por_vendedor = defaultdict(list)
    for c in cartera:
        cartera_por_vendedor[c["vendedor"]].append(c)

    resultado = {}
    vendedores = set(list(objetivos_data.keys()) + list(cartera_por_vendedor.keys()))
    for vendedor in vendedores:
        ventas_vendedor = [v for v in ventas_mes if v.get("vendedor") == vendedor]
        proveedores_con_venta = {v["proveedor"] for v in ventas_vendedor}
        proveedores_con_objetivo = set(objetivos_data.get(vendedor, {}).keys())
        todos_proveedores = proveedores_con_venta | proveedores_con_objetivo

        resultado[vendedor] = {}
        cartera_vendedor = cartera_por_vendedor.get(vendedor, [])
        clientes_cartera_total = len(cartera_vendedor)
        clientes_cartera_ids = {c["id"] for c in cartera_vendedor}

        for proveedor in todos_proveedores:
            filas_prov = [v for v in ventas_vendedor if v["proveedor"] == proveedor]
            facturacion_real = sum(f["monto_total"] for f in filas_prov)
            cajas_real = sum(f["cajas"] for f in filas_prov)
            clientes_con_compra = len({f["partner_id"] for f in filas_prov} & clientes_cartera_ids)
            cobertura_real = round(clientes_con_compra / clientes_cartera_total * 100, 1) if clientes_cartera_total > 0 else 0

            obj = objetivos_data.get(vendedor, {}).get(proveedor, {"facturacion": 0, "cajas": 0, "cobertura": 0})

            resultado[vendedor][proveedor] = {
                "facturacion_real": round(facturacion_real, 2),
                "facturacion_objetivo": obj.get("facturacion", 0),
                "cajas_real": round(cajas_real, 2),
                "cajas_objetivo": obj.get("cajas", 0),
                "clientes_con_compra": clientes_con_compra,
                "clientes_cartera": clientes_cartera_total,
                "cobertura_real": cobertura_real,
                "cobertura_objetivo": obj.get("cobertura", 0),
            }

    return resultado

@app.get("/api/objetivos/avance")
def get_objetivos_avance(mes: str):
    resultado = build_objetivos_avance(*odoo_connect(), mes)
    return {"mes": mes, "data": resultado}

@app.get("/api/pedidos")
def get_pedidos(mes: str):
    """Datos de órdenes de venta (cantidades ORDENADAS, no facturadas) para
    la sección de Pedidos en la solapa Objetivos. Permite ver el avance del
    vendedor en tiempo real sin esperar a que se facture (48hs de demora).
    Incluye: total $ por proveedor/vendedor, clientes con compra, cajas ordenadas,
    y distribución diaria ($ y clientes) para los gráficos."""
    uid, models = odoo_connect()
    uom_factors = get_uom_factors(models, uid)
    proveedores_map = get_proveedores_por_producto(models, uid)

    fecha_desde = f"{mes}-01"
    # Último día del mes
    y, m_num = int(mes[:4]), int(mes[5:7])
    if m_num == 12:
        fecha_hasta = f"{y}-12-31"
    else:
        fecha_hasta = (date(y, m_num + 1, 1) - timedelta(days=1)).strftime("%Y-%m-%d")

    ordenes = models.execute_kw(ODOO_DB, uid, ODOO_PASS,
        "sale.order", "search_read",
        [[["state", "in", ["draft", "sent", "sale", "done"]],
          ["date_order", ">=", fecha_desde],
          ["date_order", "<=", fecha_hasta]]],
        {"fields": ["id", "name", "partner_id", "date_order", "amount_total", "user_id"]}
    )
    order_ids = [o["id"] for o in ordenes]
    order_map = {o["id"]: o for o in ordenes}

    lineas = []
    if order_ids:
        lineas = models.execute_kw(ODOO_DB, uid, ODOO_PASS,
            "sale.order.line", "search_read",
            [[["order_id", "in", order_ids]]],
            {"fields": ["order_id", "product_id", "product_qty", "product_uom_id", "price_total"]}
        )

    prod_ids = list({l["product_id"][0] for l in lineas if l["product_id"]})
    prod_info = {}
    if prod_ids:
        prods = models.execute_kw(ODOO_DB, uid, ODOO_PASS,
            "product.product", "search_read",
            [[["id", "in", prod_ids]]],
            {"fields": ["id", "name", "x_studio_unidades_por_caja", "product_tmpl_id"]}
        )
        for p in prods:
            tmpl_id = p["product_tmpl_id"][0] if p.get("product_tmpl_id") else None
            prod_info[p["id"]] = {
                "unid_caja": p.get("x_studio_unidades_por_caja") or 1,
                "proveedor": proveedores_map.get(tmpl_id, "Sin proveedor"),
            }

    # Construir filas por línea de pedido
    filas = []
    for l in lineas:
        oid = l["order_id"][0] if l["order_id"] else None
        if not oid or oid not in order_map: continue
        orden = order_map[oid]
        pid = l["product_id"][0] if l["product_id"] else None
        if not pid: continue
        info = prod_info.get(pid, {"unid_caja": 1, "proveedor": "Sin proveedor"})
        # product_qty ya viene normalizado a unidades base en sale.order.line
        unidades = l["product_qty"]
        cajas = unidades / info["unid_caja"] if info["unid_caja"] > 0 else unidades
        filas.append({
            "fecha":       orden["date_order"][:10],
            "partner_id":  orden["partner_id"][0] if orden["partner_id"] else None,
            "vendedor":    orden["user_id"][1] if orden.get("user_id") else "Sin vendedor",
            "proveedor":   info["proveedor"],
            "cajas":       round(cajas, 3),
            "monto_total": round(l.get("price_total", 0), 2),
        })

    return {"mes": mes, "data": filas}


def _semaforo_color(pct):
    if pct >= 95: return (62, 207, 178)   # verde (--green)
    if pct >= 70: return (245, 200, 66)   # amarillo (--yellow)
    return (255, 79, 79)                  # rojo (--red)

def _fmt_money(n):
    return f"${n:,.0f}".replace(",", ".")

def _fmt_num(n):
    return f"{n:,.1f}".replace(",", "X").replace(".", ",").replace("X", ".")

def _cargar_fuente(size, bold=False):
    """Prioriza la fuente empaquetada junto al proyecto (static/DejaVuSans*.ttf)
    para que el resultado sea idéntico sin importar la imagen base que use
    Railway. Si por algún motivo no estuviera (ej. corriendo desde otra
    carpeta), cae a la ruta típica del sistema, y como último recurso al
    font default de Pillow para que la generación nunca falle del todo."""
    static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
    candidatos = [
        os.path.join(static_dir, "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"),
        os.path.join(static_dir, "DejaVuSans.ttf"),
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for ruta in candidatos:
        if os.path.exists(ruta):
            try:
                return ImageFont.truetype(ruta, size)
            except Exception:
                pass
    return ImageFont.load_default()

def generar_imagen_objetivos(vendedor, mes, avance_vendedor):
    """Dibuja la tabla de avance de un vendedor (Proveedor: Ventas/Objetivo/
    Cajas/Objetivo/Cobertura, con barra de semáforo) como imagen PNG,
    visualmente alineada a la paleta del dashboard."""
    BG = (11, 13, 17)
    BG2 = (19, 22, 29)
    BG3 = (26, 30, 40)
    BORDER = (37, 42, 56)
    TEXT = (232, 234, 240)
    MUTED = (92, 98, 120)
    ACCENT2 = (62, 207, 178)

    proveedores = sorted(avance_vendedor.keys())
    row_h = 64
    header_h = 110
    footer_h = 30
    width = 1120
    height = header_h + row_h * (len(proveedores) + 1) + footer_h  # +1 = fila de totales

    img = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(img)

    f_title = _cargar_fuente(26, bold=True)
    f_sub = _cargar_fuente(15)
    f_header = _cargar_fuente(12, bold=True)
    f_cell = _cargar_fuente(14)
    f_cell_b = _cargar_fuente(14, bold=True)
    f_pct = _cargar_fuente(13, bold=True)

    # Header
    draw.text((28, 22), f"Avance Objetivos — {vendedor}", font=f_title, fill=TEXT)
    draw.text((28, 58), formatMes_py(mes), font=f_sub, fill=MUTED)

    col_x = [28, 310, 460, 610, 760, 900]
    headers = ["Proveedor", "Ventas $ / Obj.", "% Vtas", "Cajas / Obj.", "% Cajas", "% Cobertura"]
    y_head = header_h - 26
    for x, h in zip(col_x, headers):
        draw.text((x, y_head), h.upper(), font=f_header, fill=MUTED)
    draw.line([(28, header_h-4), (width-28, header_h-4)], fill=BORDER, width=1)

    # Totales
    vFactReal = sum(m["facturacion_real"] for m in avance_vendedor.values())
    vFactObj  = sum(m["facturacion_objetivo"] for m in avance_vendedor.values())
    vCajasReal = sum(m["cajas_real"] for m in avance_vendedor.values())
    vCajasObj  = sum(m["cajas_objetivo"] for m in avance_vendedor.values())
    vCartera = next(iter(avance_vendedor.values()))["clientes_cartera"] if avance_vendedor else 0
    vConCompra = max((m["clientes_con_compra"] for m in avance_vendedor.values()), default=0)
    coberturaRealV = round(vConCompra / vCartera * 100, 1) if vCartera > 0 else 0
    objsCob = [m["cobertura_objetivo"] for m in avance_vendedor.values() if m["cobertura_objetivo"] > 0]
    coberturaObjV = sum(objsCob)/len(objsCob) if objsCob else 0

    def dibujar_fila(y, nombre, fact_real, fact_obj, cajas_real, cajas_obj, cob_real, cob_obj, es_total=False):
        bg = BG3 if es_total else (BG2 if (y // row_h) % 2 == 0 else BG)
        draw.rectangle([24, y, width-24, y+row_h-4], fill=bg)
        fcell = f_cell_b if es_total else f_cell
        nombre_corto = nombre if len(nombre) <= 26 else nombre[:24] + "…"
        draw.text((col_x[0]+4, y+row_h//2-10), nombre_corto, font=fcell, fill=TEXT)

        draw.text((col_x[1]+4, y+8), _fmt_money(fact_real), font=fcell, fill=TEXT)
        draw.text((col_x[1]+4, y+30), f"obj: {_fmt_money(fact_obj) if fact_obj>0 else '—'}", font=f_cell, fill=MUTED)

        pct_fact = (fact_real/fact_obj*100) if fact_obj > 0 else None
        if pct_fact is not None:
            color = _semaforo_color(pct_fact)
            draw.text((col_x[2]+4, y+18), f"{round(pct_fact)}%", font=f_pct, fill=color)
        else:
            draw.text((col_x[2]+4, y+18), "—", font=f_cell, fill=MUTED)

        draw.text((col_x[3]+4, y+8), _fmt_num(cajas_real), font=fcell, fill=TEXT)
        draw.text((col_x[3]+4, y+30), f"obj: {_fmt_num(cajas_obj) if cajas_obj>0 else '—'}", font=f_cell, fill=MUTED)

        pct_cajas = (cajas_real/cajas_obj*100) if cajas_obj > 0 else None
        if pct_cajas is not None:
            color = _semaforo_color(pct_cajas)
            draw.text((col_x[4]+4, y+18), f"{round(pct_cajas)}%", font=f_pct, fill=color)
        else:
            draw.text((col_x[4]+4, y+18), "—", font=f_cell, fill=MUTED)

        cob_txt = f"{cob_real:.1f}% / obj {cob_obj:.1f}%" if cob_obj > 0 else f"{cob_real:.1f}%"
        draw.text((col_x[5]+4, y+8), cob_txt, font=f_cell, fill=TEXT)
        pct_cob = (cob_real/cob_obj*100) if cob_obj > 0 else None
        if pct_cob is not None:
            color = _semaforo_color(pct_cob)
            draw.text((col_x[5]+4, y+30), f"{round(pct_cob)}% cumpl.", font=f_pct, fill=color)

    y = header_h
    dibujar_fila(y, "TOTAL", vFactReal, vFactObj, vCajasReal, vCajasObj, coberturaRealV, coberturaObjV, es_total=True)
    y += row_h
    for p in proveedores:
        m = avance_vendedor[p]
        dibujar_fila(y, p, m["facturacion_real"], m["facturacion_objetivo"],
                     m["cajas_real"], m["cajas_objetivo"], m["cobertura_real"], m["cobertura_objetivo"])
        y += row_h

    draw.text((28, height-24), "Kairon Distribuciones · Reporte automático diario", font=f_cell, fill=MUTED)

    return img

def formatMes_py(ym):
    meses = ["Ene","Feb","Mar","Abr","May","Jun","Jul","Ago","Sep","Oct","Nov","Dic"]
    y, m = ym.split("-")
    return f"{meses[int(m)-1]} {y}"

def enviar_reporte_whatsapp(vendedor="JK"):
    """Genera la imagen del avance del mes en curso para `vendedor` y la
    manda por WhatsApp via Twilio. Devuelve (ok: bool, detalle: str)."""
    if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_FROM, REPORTE_WHATSAPP_TO]):
        return False, "Faltan variables de entorno de Twilio (TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / TWILIO_WHATSAPP_FROM / REPORTE_WHATSAPP_TO)"

    mes = date.today().strftime("%Y-%m")
    try:
        uid, models = odoo_connect()
        avance = build_objetivos_avance(uid, models, mes)
    except Exception as e:
        return False, f"Error consultando Odoo: {e}"

    avance_vendedor = avance.get(vendedor)
    if not avance_vendedor:
        return False, f"No hay datos de avance para el vendedor '{vendedor}' en {mes}"

    try:
        img = generar_imagen_objetivos(vendedor, mes, avance_vendedor)
    except Exception as e:
        return False, f"Error generando la imagen: {e}"

    # Guardamos el PNG en static/ para que Twilio pueda descargarlo por URL pública.
    # Limpiamos reportes viejos primero (son efímeros, solo necesitan vivir
    # el tiempo que tarda Twilio en buscarlos) para no acumular archivos.
    static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
    try:
        for f in os.listdir(static_dir):
            if f.startswith("reporte_") and f.endswith(".png"):
                os.remove(os.path.join(static_dir, f))
    except Exception:
        pass

    img_filename = f"reporte_{vendedor}_{mes}_{int(time.time())}.png"
    img_path = os.path.join(static_dir, img_filename)
    img.save(img_path, "PNG")

    base_url = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
    if not base_url:
        return False, "Falta la variable de entorno PUBLIC_BASE_URL (la URL pública del dashboard, ej. https://web-production-xxxx.up.railway.app)"
    img_url = f"{base_url}/static/{img_filename}"

    try:
        client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        reporte_link = f"{base_url}/reporte/{vendedor}?mes={mes}"
        client.messages.create(
            from_=TWILIO_WHATSAPP_FROM,
            to=REPORTE_WHATSAPP_TO,
            body=f"📊 Avance de objetivos — {vendedor} — {formatMes_py(mes)}\nVer detalle: {reporte_link}",
            media_url=[img_url],
        )
    except Exception as e:
        return False, f"Error enviando por Twilio: {e}"

    return True, f"Reporte de {vendedor} enviado correctamente ({img_filename})"

@app.get("/api/reporte-diario")
def trigger_reporte_diario(secret: str = "", vendedor: str = "JK"):
    """Endpoint que dispara el cron externo (ej. cron-job.org) todos los
    días a las 8 AM. Protegido con un secret simple en query param."""
    if REPORTE_CRON_SECRET and secret != REPORTE_CRON_SECRET:
        raise HTTPException(status_code=403, detail="Secret inválido")
    ok, detalle = enviar_reporte_whatsapp(vendedor)
    if not ok:
        raise HTTPException(status_code=500, detail=detalle)
    return {"ok": True, "detalle": detalle}

@app.get("/api/status")
def status():
    return {
        "ok": True,
        # Bloque de diagnostico. /api/status es la unica ruta que queda
        # publica, asi que es la unica forma de ver desde afuera si el
        # contenedor que esta corriendo trae Access activo o no. No expone
        # nada sensible: el AUD va cortado y el team domain ya es publico.
        "access": {
            "activo": access_configurado(),
            "team_domain": CF_ACCESS_TEAM_DOMAIN,
            "aud_termina_en": CF_ACCESS_AUD[-6:] if CF_ACCESS_AUD else "",
            "aud_desde_env": bool(os.getenv("CF_ACCESS_AUD") is not None),
            "commit": os.getenv("RAILWAY_GIT_COMMIT_SHA", "")[:7],
        },
        "administracion": {
            "version": "2026-09-08.1",
            "modo": "compra-confirmada-factura-borrador",
            "odoo_configurado": bool(ODOO_URL and ODOO_DB and ODOO_USER and ODOO_PASS),
            "volumen_datos": os.path.ismount("/data"),
        },
        "odoo_url": ODOO_URL,
        "cache_ttl": CACHE_TTL,
        "cached_keys": list(_cache.keys()),
        "timestamp": datetime.now().isoformat(),
        "twilio_configurado": {
            "TWILIO_ACCOUNT_SID": bool(TWILIO_ACCOUNT_SID),
            "TWILIO_AUTH_TOKEN": bool(TWILIO_AUTH_TOKEN),
            "TWILIO_WHATSAPP_FROM": bool(TWILIO_WHATSAPP_FROM),
            "REPORTE_WHATSAPP_TO": bool(REPORTE_WHATSAPP_TO),
            "PUBLIC_BASE_URL": bool(os.getenv("PUBLIC_BASE_URL", "")),
            "REPORTE_CRON_SECRET": bool(REPORTE_CRON_SECRET),
        },
        "test_var_diagnostico": os.getenv("TEST_VAR", "NO_ENCONTRADA")
    }

@app.get("/api/cobranzas")
def get_cobranzas(force: bool = False):
    cached = cache_get("cobranzas")
    if cached and not force:
        return {"data": cached, "cached": True, "ttl": CACHE_TTL}
    uid, models = odoo_connect()
    data = build_cobranzas_data(uid, models)
    cache_set("cobranzas", data)
    return {"data": data, "cached": False, "ttl": CACHE_TTL}

@app.get("/api/dias-habiles")
def get_dias_habiles():
    """Días hábiles (lunes-viernes, excluyendo feriados nacionales AR) del
    mes actual: transcurridos (incluyendo hoy), restantes y total del mes."""
    transcurridos, restantes, total = dias_habiles_transcurridos_y_restantes()
    return {
        "transcurridos": transcurridos,
        "restantes": restantes,
        "total_mes": total,
        "hoy": date.today().isoformat(),
    }

@app.get("/api/refresh")
def refresh_all():
    uid, models = odoo_connect()
    resultados = {}
    builders = {
        "stock":     build_stock_data,
        "ventas":    build_ventas_data,
        "clientes":  build_clientes_data,
        "cartera":   build_cartera_data,
        "cobranzas": build_cobranzas_data,
    }
    for key, builder in builders.items():
        try:
            cache_set(key, builder(uid, models))
            resultados[key] = "ok"
        except Exception as e:
            # Si un módulo falla, los demás se siguen actualizando igual.
            # El caché previo de este módulo queda intacto (no se pisa con error).
            resultados[key] = f"error: {str(e)[:200]}"

    ok_general = all(v == "ok" for v in resultados.values())
    return {"ok": ok_general, "refreshed_at": datetime.now().isoformat(), "detalle": resultados}

# ─── Mapa de clientes ───────────────────────────────────────
# Códigos de producto que representan un exhibidor. Si el cliente compró
# alguno alguna vez, damos por hecho que lo tiene colocado.
EXHIBIDOR_CODES = {"111", "112"}

# Campo de Studio donde vive el dia de visita del cliente. Es un
# many2many, asi que trae ids y hay que resolverlos contra su modelo.
# Tener dia de visita cargado es lo que define que el cliente sea parte
# de la cartera: es, literalmente, "lo visito".
CAMPO_DIA_VISITA = "x_studio_many2many_field_4bq_1j6ma1s65"

# Geo de los clientes (código, lat/lon, canal, zonas). Viene del maestro de
# QuadMinds: es la fuente con coordenadas completas para los 806 puntos.
# Odoo aporta la parte comercial y, si tiene coordenadas propias cargadas,
# se prefieren esas.
_DATA_STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "data")
_GEO_PATH    = os.path.join(_DATA_STATIC, "clientes_geo.json")
_ZONAS_PATH  = os.path.join(_DATA_STATIC, "zonas.geojson")
_geo_cache = None
_zonas_cache = None


def cargar_zonas():
    """Anillos de las 5 zonas de venta, para ubicar un punto."""
    global _zonas_cache
    if _zonas_cache is None:
        with open(_ZONAS_PATH, "r", encoding="utf-8") as f:
            gj = json.load(f)
        _zonas_cache = [(f["properties"]["nombre"], f["geometry"]["coordinates"][0])
                        for f in gj["features"]]
    return _zonas_cache


def zona_de(lon, lat):
    """Zona que contiene al punto (ray casting). La zona se calcula acá y no
    en el maestro porque las coordenadas pueden venir de Odoo: si se usara la
    precalculada, un cliente podría quedar dibujado dentro de un polígono y
    etiquetado con otro."""
    for nombre, anillo in cargar_zonas():
        dentro = False
        for i in range(len(anillo) - 1):
            x1, y1 = anillo[i]
            x2, y2 = anillo[i + 1]
            if (y1 > lat) != (y2 > lat) and lon < x1 + (lat - y1) / (y2 - y1) * (x2 - x1):
                dentro = not dentro
        if dentro:
            return nombre
    return "Fuera de zona"


def _sin_acentos(s):
    return unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower()

def cargar_geo_clientes():
    global _geo_cache
    if _geo_cache is None:
        with open(_GEO_PATH, "r", encoding="utf-8") as f:
            _geo_cache = json.load(f)
    return _geo_cache


def _norm_cod(v):
    """Normaliza un código de cliente para comparar entre Odoo y el maestro.
    En Odoo es texto y puede venir con espacios o con .0 si alguna vez pasó
    por una planilla; en el maestro es entero."""
    if v is None or v is False:
        return ""
    s = str(v).strip()
    if s.endswith(".0"):
        s = s[:-2]
    return s.lstrip("0") or "0"


def build_mapa_data(uid, models, meses_n=12):
    """Cruza el maestro geo con la actividad comercial de Odoo.

    El vínculo entre ambos lados es el código de cliente. En Odoo vive en
    'ref' salvo que la instancia use un campo de Studio, así que se detecta
    cuál de los candidatos existe realmente antes de pedirlo — pedir un
    campo inexistente hace fallar todo el search_read."""
    geo = cargar_geo_clientes()

    # Qué campos de res.partner existen de verdad en esta instancia.
    meta = models.execute_kw(ODOO_DB, uid, ODOO_PASS,
        "res.partner", "fields_get", [], {"attributes": ["type", "relation"]})
    disponibles = set(meta.keys())

    CAND_COD = ["ref", "x_studio_codigo", "x_studio_codigo_cliente", "x_studio_cod_cliente"]
    campos_cod = [c for c in CAND_COD if c in disponibles]
    campos_geo = [c for c in ("partner_latitude", "partner_longitude") if c in disponibles]

    fields = ["id", "name", "user_id"] + campos_cod + campos_geo
    for c in ("x_studio_canal", "x_studio_tipo_de_comercio", CAMPO_DIA_VISITA):
        if c in disponibles:
            fields.append(c)

    partners = models.execute_kw(ODOO_DB, uid, ODOO_PASS,
        "res.partner", "search_read",
        [[["customer_rank", ">", 0], ["active", "=", True]]],
        {"fields": fields, "limit": 20000}
    )

    # Cartera = tiene día de visita cargado. Si el campo no existe en la
    # instancia se cae al criterio que ya usa el resto del dashboard
    # (tener alguna venta), y el diagnóstico dice cuál se aplicó.
    hay_dia = CAMPO_DIA_VISITA in disponibles
    dias_nombre = {}
    if hay_dia:
        rel = (meta[CAMPO_DIA_VISITA] or {}).get("relation")
        ids = {i for p in partners for i in (p.get(CAMPO_DIA_VISITA) or [])}
        if rel and ids:
            for d in models.execute_kw(ODOO_DB, uid, ODOO_PASS, rel, "read",
                                       [sorted(ids)], {"fields": ["display_name"]}):
                dias_nombre[d["id"]] = d.get("display_name") or str(d["id"])
        cartera_ids = {p["id"] for p in partners if p.get(CAMPO_DIA_VISITA)}
        criterio = "dia_de_visita"
    else:
        cartera_ids = set(models.execute_kw(ODOO_DB, uid, ODOO_PASS,
            "res.partner", "search",
            [[["sale_order_ids", "!=", False]]], {"limit": 50000}))
        criterio = "tiene_ventas"

    # Índices para el cruce: por código y, como red, por nombre normalizado.
    por_cod, por_nom = {}, {}
    for p in partners:
        for c in campos_cod:
            k = _norm_cod(p.get(c))
            if k and k not in por_cod:
                por_cod[k] = p
        n = (p.get("name") or "").strip().upper()
        if n and n not in por_nom:
            por_nom[n] = p

    # Ventana de meses a analizar.
    hoy = date.today()
    primer_mes = (hoy.replace(day=1) - timedelta(days=31 * (meses_n - 1))).replace(day=1)
    desde = primer_mes.strftime("%Y-%m-%d")

    ordenes = models.execute_kw(ODOO_DB, uid, ODOO_PASS,
        "sale.order", "search_read",
        [[["state", "in", ["sale", "done"]], ["date_order", ">=", desde]]],
        {"fields": ["id", "partner_id", "date_order"], "limit": 100000}
    )
    orden_info = {o["id"]: o for o in ordenes}

    lineas = []
    if ordenes:
        lineas = models.execute_kw(ODOO_DB, uid, ODOO_PASS,
            "sale.order.line", "search_read",
            [[["order_id", "in", list(orden_info.keys())]]],
            {"fields": ["order_id", "product_id", "price_subtotal", "product_uom_qty"],
             "limit": 400000}
        )

    # Producto -> template (para el proveedor) y default_code (para exhibidor).
    prod_ids = list({l["product_id"][0] for l in lineas if l.get("product_id")})
    prod_info = {}
    if prod_ids:
        for p in models.execute_kw(ODOO_DB, uid, ODOO_PASS,
                "product.product", "search_read",
                [[["id", "in", prod_ids]]],
                {"fields": ["id", "default_code", "product_tmpl_id"], "limit": 50000}):
            prod_info[p["id"]] = {
                "code": (p.get("default_code") or "").strip(),
                "tmpl": p["product_tmpl_id"][0] if p.get("product_tmpl_id") else None,
            }
    proveedores_map = get_proveedores_por_producto(models, uid)

    # Agregación por partner y por mes.
    acum = defaultdict(lambda: {"meses": defaultdict(lambda: {"fact": 0.0, "ops": set(), "provs": defaultdict(float)}),
                                "exhibidor": False})
    for l in lineas:
        o = orden_info.get(l["order_id"][0] if l.get("order_id") else None)
        if not o or not o.get("partner_id"):
            continue
        pid = o["partner_id"][0]
        mes = o["date_order"][:7]
        pi = prod_info.get(l["product_id"][0]) if l.get("product_id") else None
        reg = acum[pid]
        m = reg["meses"][mes]
        m["fact"] += l.get("price_subtotal") or 0.0
        m["ops"].add(o["id"])
        if pi:
            if pi["code"] in EXHIBIDOR_CODES:
                reg["exhibidor"] = True
            prov = proveedores_map.get(pi["tmpl"], "Sin proveedor")
            m["provs"][prov] += l.get("price_subtotal") or 0.0

    meses = sorted({o["date_order"][:7] for o in ordenes})
    proveedores = sorted({p for r in acum.values() for m in r["meses"].values() for p in m["provs"]})

    # El cruce tiene que ser 1 a 1. Sin esto, dos fichas del maestro que
    # comparten nombre (una sucursal cargada dos veces, por ejemplo) matchean
    # contra el mismo partner y sus ventas se cuentan dos veces en los KPIs.
    asignado, usados = {}, set()
    for g in geo:                                   # 1ª pasada: por código
        p = por_cod.get(_norm_cod(g["cod"]))
        if p and p["id"] not in usados:
            asignado[g["cod"]] = p
            usados.add(p["id"])
    for g in geo:                                   # 2ª pasada: por nombre
        if g["cod"] in asignado:
            continue
        p = por_nom.get((g["nom"] or "").strip().upper())
        if p and p["id"] not in usados:
            asignado[g["cod"]] = p
            usados.add(p["id"])

    salida, matcheados = [], 0
    for g in geo:
        p = asignado.get(g["cod"])
        item = {
            "cod": g["cod"], "nom": g["nom"], "dir": g["dir"],
            "canal": g["canal"], "lat": g["lat"], "lon": g["lon"],
            "zona": g["zona"], "odoo_id": None, "vendedor": None,
            "cartera": False, "dias_visita": [], "exhibidor": False, "meses": {},
        }
        if p:
            matcheados += 1
            item["odoo_id"] = p["id"]
            item["cartera"] = p["id"] in cartera_ids
            item["dias_visita"] = [dias_nombre.get(i, str(i))
                                   for i in (p.get(CAMPO_DIA_VISITA) or [])]
            item["vendedor"] = p["user_id"][1] if p.get("user_id") else None
            # Si Odoo tiene coordenadas propias cargadas, mandan esas.
            la, lo = p.get("partner_latitude"), p.get("partner_longitude")
            if la and lo:
                item["lat"], item["lon"] = round(la, 6), round(lo, 6)
                item["zona"] = zona_de(item["lon"], item["lat"])
            if p.get("x_studio_canal"):
                item["canal_odoo"] = p["x_studio_canal"]
            reg = acum.get(p["id"])
            if reg:
                item["exhibidor"] = reg["exhibidor"]
                item["meses"] = {
                    mes: {"fact": round(v["fact"], 2), "ops": len(v["ops"]),
                          "provs": {k: round(x, 2) for k, x in v["provs"].items()}}
                    for mes, v in reg["meses"].items()
                }
        salida.append(item)

    return {
        "clientes": salida,
        "meses": meses,
        "proveedores": proveedores,
        "diagnostico": {
            "geo_total": len(geo),
            "odoo_partners": len(partners),
            "matcheados": matcheados,
            "sin_match": len(geo) - matcheados,
            "en_cartera": sum(1 for c in salida if c["cartera"]),
            "criterio_cartera": criterio,
            "campo_dia_visita": CAMPO_DIA_VISITA if hay_dia else None,
            "con_dia_visita": sum(1 for c in salida if c["dias_visita"]),
            # Cuántos tienen el día de Odoo de acuerdo con la zona en la que
            # caen geométricamente. Se comparan sin acentos porque Odoo los
            # escribe sin ellos ("Miercoles") y el KML con ("Miércoles").
            "dia_coincide_con_zona": sum(
                1 for c in salida if c["dias_visita"] and
                any(_sin_acentos(c["zona"]) == _sin_acentos(d) for d in c["dias_visita"])),
            "dia_difiere_de_zona": sum(
                1 for c in salida if c["dias_visita"] and
                not any(_sin_acentos(c["zona"]) == _sin_acentos(d) for d in c["dias_visita"])),
            # Clientes de Odoo que no están en el maestro: no tienen
            # coordenadas, así que hoy no se ven en el mapa.
            "odoo_sin_geo": len(partners) - matcheados,
            # Compran pero no tienen día de visita cargado.
            "compran_sin_dia_visita": sum(
                1 for c in salida if c["meses"] and not c["cartera"]),
            "campos_codigo_detectados": campos_cod,
            "coords_odoo_disponibles": bool(campos_geo),
            "exhibidor_codes": sorted(EXHIBIDOR_CODES),
        },
    }


@app.get("/api/mapa/clientes")
def api_mapa_clientes(force: bool = False):
    key = "mapa_clientes"
    if not force:
        cached = cache_get(key)
        if cached:
            return {"data": cached, "cached": True}
    uid, models = odoo_connect()
    data = build_mapa_data(uid, models)
    cache_set(key, data)
    return {"data": data, "cached": False}


# ─── Agente de ventas · WhatsApp ─────────────────────────────
# Módulo para armar la cartera de clientes a contactar por WhatsApp:
# trae los clientes de Odoo, los agrupa por día de visita, los ubica en
# un mapa y deja marcar manualmente a quién se contacta. El resultado se
# exporta como planilla con hipervínculos wa.me.

AGENTE_PATH = os.path.join(_DATA_DIR, "agente_config.json")
_agente_lock = threading.Lock()

MENSAJE_DEFAULT = (
    "Hola {contacto}! Te escribo de Kairon. "
    "Te paso el catálogo actualizado por si querés armar el pedido de esta semana. "
    "¿Te tomo algo?"
)

# Campo de res.partner que guarda el día de visita. En Kairon es un many2many
# creado con Studio (un cliente puede tener más de un día: "Lunes y Jueves").
# Se puede pisar por variable de entorno o cambiar desde la UI (paso 1).
# CAMPO_DIA_VISITA lo define el módulo Mapas, que usa el mismo campo.
CAMPO_DIA_VISITA_DEFAULT = os.getenv("CAMPO_DIA_VISITA", CAMPO_DIA_VISITA)

# Campo de res.partner con el nombre de la persona con la que se habla (el
# dueño del comercio o el encargado), que no es la razón social del cliente.
# Vacío = se detecta solo buscando el campo cuya etiqueta es "Persona de
# contacto"; se puede fijar con la variable de entorno CAMPO_CONTACTO.
CAMPO_CONTACTO_DEFAULT = os.getenv("CAMPO_CONTACTO", "")

AGENTE_CONFIG_DEFAULT = {
    "campo_dia_visita": CAMPO_DIA_VISITA_DEFAULT,
    "campo_contacto": CAMPO_CONTACTO_DEFAULT,
    "mensaje": MENSAJE_DEFAULT,
    "seleccionados": [],   # ids de res.partner marcados para contactar
    "geo": {},             # {"<partner_id>": [lat, lon]} geocodificados acá
}


def cargar_agente_config():
    with _agente_lock:
        cfg = dict(AGENTE_CONFIG_DEFAULT)
        if os.path.exists(AGENTE_PATH):
            try:
                with open(AGENTE_PATH, "r", encoding="utf-8") as f:
                    cfg.update(json.load(f) or {})
            except Exception:
                pass
        if not (cfg.get("campo_dia_visita") or "").strip():
            cfg["campo_dia_visita"] = CAMPO_DIA_VISITA_DEFAULT
        cfg["seleccionados"] = [int(x) for x in cfg.get("seleccionados", [])]
        cfg["geo"] = {str(k): v for k, v in (cfg.get("geo") or {}).items()}
        return cfg


def guardar_agente_config(cfg):
    with _agente_lock:
        with open(AGENTE_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)


# ─── Teléfonos ───────────────────────────────────────────────
def _limpiar_nacional_ar(n):
    """Saca el 0 de larga distancia y el 15 de celular: 011 15 3230-8807 → 1132308807"""
    if n.startswith("0"):
        n = n[1:]
    for corte in (2, 3, 4):
        if len(n) > corte + 2 and n[corte:corte + 2] == "15":
            cand = n[:corte] + n[corte + 2:]
            if len(cand) == 10:
                return cand
    return n


def normalizar_tel_ar(raw):
    """Devuelve (numero_e164_sin_mas, ok). ok=False cuando quedó un número
    con largo raro: se muestra igual en la UI pero marcado para revisar."""
    if not raw:
        return "", False
    digits = re.sub(r"\D", "", str(raw))
    if not digits:
        return "", False
    if digits.startswith("00"):
        digits = digits[2:]
    if digits.startswith("54"):
        resto = digits[2:]
        if resto.startswith("9"):
            resto = resto[1:]
        resto = _limpiar_nacional_ar(resto)
    else:
        resto = _limpiar_nacional_ar(digits)
    if not resto:
        return "", False
    return "549" + resto, len(resto) == 10


def _primer_telefono(p):
    for campo in ("mobile", "phone"):
        crudo = p.get(campo)
        if crudo:
            num, ok = normalizar_tel_ar(crudo)
            if num:
                return {"crudo": str(crudo).strip(), "campo": campo, "e164": num, "ok": ok}
    return {"crudo": "", "campo": "", "e164": "", "ok": False}


# ─── Descubrimiento del campo "día de visita" ────────────────
_RX_CONTACTO = re.compile(r"persona\s*de\s*contacto|contact[oa]\b|encargad|due[nñ]", re.IGNORECASE)
_TIPOS_TEXTO = ("char", "text", "many2one")


def _detectar_campo_contacto(campos_meta):
    """Los campos de Studio tienen nombre técnico ilegible, así que la persona
    de contacto se busca por etiqueta: primero la exacta, después parecidas."""
    candidatos = [(n, (m.get("string") or "").strip().lower())
                  for n, m in campos_meta.items()
                  if m.get("type") in _TIPOS_TEXTO and n not in ("name", "display_name")]
    for n, etiqueta in candidatos:
        if etiqueta == "persona de contacto":
            return n
    for n, etiqueta in candidatos:
        if n.startswith("x_") and _RX_CONTACTO.search(etiqueta):
            return n
    return ""


_RX_DIA_VISITA = re.compile(
    r"visit|d[ií]a|day|ruta|route|reparto|frecuen|jornada|zona|recorrid|preventa",
    re.IGNORECASE,
)
_TIPOS_AGRUPABLES = ("selection", "char", "many2one", "many2many", "one2many", "boolean", "integer")


def _fields_partner(models, uid):
    return models.execute_kw(
        ODOO_DB, uid, ODOO_PASS, "res.partner", "fields_get", [],
        {"attributes": ["string", "type", "selection", "relation", "store"]},
    )


@app.get("/api/agente/campos")
def get_agente_campos():
    """Paso 1: identificar juntos el campo del día de visita.
    Lista los campos de res.partner que suenan a día/ruta/visita, con los
    valores que hoy tienen cargados los clientes y cuántos hay en cada uno,
    así se elige el correcto sin adivinar."""
    uid, models = odoo_connect()
    campos = _fields_partner(models, uid)

    candidatos = []
    for name, meta in campos.items():
        tipo = meta.get("type")
        if tipo not in _TIPOS_AGRUPABLES:
            continue
        etiqueta = meta.get("string") or name
        es_custom = name.startswith("x_")
        suena = (_RX_DIA_VISITA.search(name) or _RX_DIA_VISITA.search(etiqueta)
                 or _RX_CONTACTO.search(etiqueta))
        if not (es_custom or suena):
            continue
        candidatos.append({
            "name": name,
            "string": etiqueta,
            "type": tipo,
            "relation": meta.get("relation") or "",
            "selection": [{"valor": v, "etiqueta": l} for v, l in (meta.get("selection") or [])],
            "es_custom": es_custom,
            "valores": [],
        })

    # Para cada candidato, qué valores hay cargados hoy y en cuántos clientes.
    dominio_cli = [["customer_rank", ">", 0], ["active", "=", True]]
    for c in candidatos[:25]:
        try:
            grupos = models.execute_kw(
                ODOO_DB, uid, ODOO_PASS, "res.partner", "read_group",
                [dominio_cli, [c["name"]], [c["name"]]], {"lazy": True},
            )
            valores = []
            for g in grupos:
                v = g.get(c["name"])
                if isinstance(v, (list, tuple)) and len(v) == 2:
                    v = v[1]
                valores.append({
                    "valor": "(vacío)" if v in (False, None, "") else str(v),
                    "cantidad": g.get("__count") or g.get(c["name"] + "_count") or 0,
                })
            valores.sort(key=lambda x: -x["cantidad"])
            c["valores"] = valores[:20]
        except Exception as e:
            c["valores"] = []
            c["error"] = str(e)[:160]

    # Las etiquetas de contacto son la otra forma habitual de marcar el día.
    etiquetas = []
    try:
        cats = models.execute_kw(
            ODOO_DB, uid, ODOO_PASS, "res.partner.category", "search_read",
            [[]], {"fields": ["id", "name"], "limit": 200},
        )
        etiquetas = [{"id": c["id"], "name": c["name"]} for c in cats]
    except Exception:
        pass

    # Orden: primero los campos custom (x_studio_...), que es donde suele estar.
    candidatos.sort(key=lambda c: (not c["es_custom"], -sum(v["cantidad"] for v in c["valores"])))

    return {
        "candidatos": candidatos,
        "etiquetas": etiquetas,
        "config": cargar_agente_config(),
        "total_campos": len(campos),
        "campo_contacto_detectado": _detectar_campo_contacto(campos),
    }


class AgenteConfigBody(BaseModel):
    campo_dia_visita: str = None
    campo_contacto: str = None
    mensaje: str = None


@app.post("/api/agente/config")
def post_agente_config(body: AgenteConfigBody):
    cfg = cargar_agente_config()
    if body.campo_dia_visita is not None:
        cfg["campo_dia_visita"] = body.campo_dia_visita.strip()
    if body.campo_contacto is not None:
        cfg["campo_contacto"] = body.campo_contacto.strip()
    if body.mensaje is not None:
        cfg["mensaje"] = body.mensaje
    guardar_agente_config(cfg)
    cache_del("agente_clientes")
    return {"ok": True, "config": cfg}


class SeleccionBody(BaseModel):
    seleccionados: list[int]


@app.post("/api/agente/seleccion")
def post_agente_seleccion(body: SeleccionBody):
    cfg = cargar_agente_config()
    cfg["seleccionados"] = sorted({int(x) for x in body.seleccionados})
    guardar_agente_config(cfg)
    return {"ok": True, "cantidad": len(cfg["seleccionados"])}


# ─── Clientes del agente ─────────────────────────────────────
_ORDEN_SEMANA = ["lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo"]


def _orden_dia(dia):
    """Lunes antes que Martes: ordenar alfabéticamente dejaría Jueves primero."""
    d = (dia or "").strip().lower()
    d = (d.replace("á", "a").replace("é", "e").replace("í", "i")
          .replace("ó", "o").replace("ú", "u"))
    for i, nombre in enumerate(_ORDEN_SEMANA):
        if d.startswith(nombre[:4]):
            return (0, i, d)
    return (1, 0, d)


def _dias_de_valor(valor, nombres_rel):
    """Normaliza el valor del campo día de visita a una lista de etiquetas,
    sirva el campo un solo día (selection/char/many2one) o varios (many2many)."""
    if valor in (False, None, ""):
        return []
    if isinstance(valor, (list, tuple)):
        if len(valor) == 2 and isinstance(valor[0], int) and isinstance(valor[1], str):
            return [valor[1]]  # many2one
        return [nombres_rel.get(v, str(v)) for v in valor]  # many2many / one2many
    texto = str(valor).strip()
    if not texto:
        return []
    partes = [t.strip() for t in re.split(r"[,;/|]| y ", texto) if t.strip()]
    return partes or [texto]


def build_agente_clientes(uid, models):
    cfg = cargar_agente_config()
    campo = (cfg.get("campo_dia_visita") or "").strip()
    campos_meta = _fields_partner(models, uid)

    base = ["id", "name", "phone", "mobile", "email", "street", "street2", "city",
            "zip", "state_id", "partner_latitude", "partner_longitude",
            "category_id", "user_id", "vat"]
    pedir = [f for f in base if f in campos_meta]
    campo_ok = bool(campo) and campo in campos_meta
    if campo_ok and campo not in pedir:
        pedir.append(campo)

    # Persona de contacto: la configurada, o la que se detecte por etiqueta.
    campo_cto = (cfg.get("campo_contacto") or "").strip()
    if not campo_cto or campo_cto not in campos_meta:
        campo_cto = _detectar_campo_contacto(campos_meta)
    if campo_cto and campo_cto not in pedir:
        pedir.append(campo_cto)

    partners = odoo_call(
        models, uid, "res.partner", "search_read",
        [["customer_rank", ">", 0], ["active", "=", True]],
        pedir, limit=20000,
    )

    # Si el día de visita vive en un campo relacional, resolvemos los nombres.
    nombres_rel = {}
    if campo_ok and campos_meta[campo].get("type") in ("many2many", "one2many"):
        rel = campos_meta[campo].get("relation")
        ids_rel = set()
        for p in partners:
            v = p.get(campo)
            if isinstance(v, list):
                ids_rel.update(v)
        if rel and ids_rel:
            try:
                regs = models.execute_kw(
                    ODOO_DB, uid, ODOO_PASS, rel, "read",
                    [sorted(ids_rel)], {"fields": ["display_name"]},
                )
                nombres_rel = {r["id"]: r.get("display_name") or str(r["id"]) for r in regs}
            except Exception:
                pass

    # Nombres de las etiquetas de contacto (sirven como agrupador extra).
    nombres_cat = {}
    ids_cat = {t for p in partners for t in (p.get("category_id") or [])}
    if ids_cat:
        try:
            cats = models.execute_kw(
                ODOO_DB, uid, ODOO_PASS, "res.partner.category", "read",
                [sorted(ids_cat)], {"fields": ["name"]},
            )
            nombres_cat = {c["id"]: c["name"] for c in cats}
        except Exception:
            pass

    # Última compra (365 días) para saber a quién conviene escribirle.
    ultima_compra = {}
    try:
        desde = (date.today() - timedelta(days=365)).strftime("%Y-%m-%d")
        ordenes = odoo_call(
            models, uid, "sale.order", "search_read",
            [["state", "in", ["sale", "done"]], ["date_order", ">=", desde]],
            ["partner_id", "date_order", "amount_total"], limit=50000,
        )
        for o in ordenes:
            if not o.get("partner_id"):
                continue
            pid = o["partner_id"][0]
            reg = ultima_compra.setdefault(pid, {"fecha": None, "monto_365d": 0.0, "ordenes": 0})
            reg["monto_365d"] += o.get("amount_total") or 0
            reg["ordenes"] += 1
            f = (o.get("date_order") or "")[:10]
            if f and (reg["fecha"] is None or f > reg["fecha"]):
                reg["fecha"] = f
    except Exception:
        pass

    geo_manual = cfg.get("geo") or {}
    seleccionados = set(cfg.get("seleccionados") or [])
    hoy = date.today()
    out = []
    for p in partners:
        pid = p["id"]
        dias = sorted(_dias_de_valor(p.get(campo) if campo_ok else None, nombres_rel), key=_orden_dia)
        tel = _primer_telefono(p)

        contacto = p.get(campo_cto) if campo_cto else None
        if isinstance(contacto, (list, tuple)):
            contacto = contacto[1] if len(contacto) == 2 else ""
        contacto = (contacto or "").strip() if isinstance(contacto, str) else ""

        lat = p.get("partner_latitude") or 0
        lon = p.get("partner_longitude") or 0
        origen_geo = "odoo"
        if not lat or not lon:
            man = geo_manual.get(str(pid))
            if man:
                lat, lon, origen_geo = man[0], man[1], "geocodificado"
            else:
                lat, lon, origen_geo = 0, 0, ""

        uc = ultima_compra.get(pid, {})
        dias_sin_comprar = None
        if uc.get("fecha"):
            try:
                dias_sin_comprar = (hoy - date.fromisoformat(uc["fecha"])).days
            except Exception:
                pass

        direccion = ", ".join([x for x in [p.get("street"), p.get("street2"), p.get("city"),
                                           (p.get("state_id") or [None, ""])[1] if p.get("state_id") else ""] if x])

        out.append({
            "id": pid,
            "nombre": p.get("name") or "",
            "contacto": contacto,
            "dias_visita": dias,
            "telefono": tel["e164"],
            "telefono_crudo": tel["crudo"],
            "telefono_campo": tel["campo"],
            "telefono_ok": tel["ok"],
            "email": p.get("email") or "",
            "direccion": direccion,
            "ciudad": p.get("city") or "",
            "lat": lat,
            "lon": lon,
            "origen_geo": origen_geo,
            "vendedor": (p.get("user_id") or [None, ""])[1] if p.get("user_id") else "",
            "etiquetas": [nombres_cat.get(t, str(t)) for t in (p.get("category_id") or [])],
            "ultima_compra": uc.get("fecha"),
            "dias_sin_comprar": dias_sin_comprar,
            "monto_365d": round(uc.get("monto_365d", 0.0), 2),
            "seleccionado": pid in seleccionados,
        })

    out.sort(key=lambda c: (-(c["monto_365d"] or 0), c["nombre"]))

    dias_disponibles = sorted({d for c in out for d in c["dias_visita"]}, key=_orden_dia)
    return {
        "clientes": out,
        "dias": dias_disponibles,
        "config": cfg,
        "campo_configurado": campo_ok,
        "campo_dia_visita": campo if campo_ok else "",
        "campo_etiqueta": (campos_meta.get(campo, {}) or {}).get("string", "") if campo_ok else "",
        "campo_contacto": campo_cto,
        "campo_contacto_etiqueta": (campos_meta.get(campo_cto, {}) or {}).get("string", "") if campo_cto else "",
        "resumen": {
            "total": len(out),
            "con_telefono": sum(1 for c in out if c["telefono"]),
            "sin_telefono": sum(1 for c in out if not c["telefono"]),
            "telefono_dudoso": sum(1 for c in out if c["telefono"] and not c["telefono_ok"]),
            "sin_geo": sum(1 for c in out if not c["lat"] or not c["lon"]),
            "sin_dia": sum(1 for c in out if not c["dias_visita"]),
            "sin_contacto": sum(1 for c in out if not c["contacto"]),
            "seleccionados": sum(1 for c in out if c["seleccionado"]),
        },
    }


@app.get("/api/agente/clientes")
def get_agente_clientes(force: bool = False):
    if not force:
        cached = cache_get("agente_clientes")
        if cached:
            # La selección se guarda aparte: siempre se refresca sobre el caché.
            cfg = cargar_agente_config()
            sel = set(cfg.get("seleccionados") or [])
            for c in cached["clientes"]:
                c["seleccionado"] = c["id"] in sel
            cached["resumen"]["seleccionados"] = len(
                [c for c in cached["clientes"] if c["seleccionado"]])
            cached["config"] = cfg
            return cached
    uid, models = odoo_connect()
    data = build_agente_clientes(uid, models)
    cache_set("agente_clientes", data)
    return data


# ─── Geocodificación de los que no tienen coordenadas ────────
def _geocodificar_direccion(texto):
    url = ("https://nominatim.openstreetmap.org/search?format=json&limit=1&countrycodes=ar&q="
           + urllib.parse.quote(texto))
    req = urllib.request.Request(url, headers={"User-Agent": "kairon-dashboard/1.0 (agente de ventas)"})
    with urllib.request.urlopen(req, timeout=12) as r:
        data = json.loads(r.read().decode("utf-8"))
    if not data:
        return None
    return [float(data[0]["lat"]), float(data[0]["lon"])]


@app.post("/api/agente/geocodificar")
def post_agente_geocodificar(limite: int = 20):
    """Busca coordenadas en OpenStreetMap para los clientes que no las tienen
    en Odoo. Se hace de a tandas (Nominatim pide ~1 consulta por segundo)."""
    data = get_agente_clientes()
    cfg = cargar_agente_config()
    geo = cfg.get("geo") or {}
    pendientes = [c for c in data["clientes"]
                  if (not c["lat"] or not c["lon"]) and c["direccion"] and str(c["id"]) not in geo]
    hechos, fallados = 0, 0
    for c in pendientes[:max(1, min(limite, 50))]:
        try:
            coord = _geocodificar_direccion(c["direccion"])
            if coord:
                geo[str(c["id"])] = coord
                hechos += 1
            else:
                fallados += 1
        except Exception:
            fallados += 1
        time.sleep(1.1)
    cfg["geo"] = geo
    guardar_agente_config(cfg)
    cache_del("agente_clientes")
    return {"ok": True, "geocodificados": hechos, "sin_resultado": fallados,
            "pendientes": max(0, len(pendientes) - hechos - fallados)}


# ─── Exportable con hipervínculos de WhatsApp ────────────────
def _link_whatsapp(tel, nombre, dias, mensaje, contacto=""):
    """{contacto} es la persona con la que se habla (dueño o encargado). Si el
    cliente no la tiene cargada, cae en el nombre del comercio para que el
    mensaje nunca salga con un hueco."""
    texto = (mensaje or MENSAJE_DEFAULT)
    quien = (contacto or "").strip() or (nombre or "")
    texto = texto.replace("{contacto}", quien)
    texto = texto.replace("{primer_nombre_contacto}", quien.split(" ")[0])
    texto = texto.replace("{nombre}", nombre or "")
    texto = texto.replace("{nombre_completo}", nombre or "")
    texto = texto.replace("{primer_nombre}", (nombre or "").split(" ")[0])
    texto = texto.replace("{dia}", ", ".join(dias) if dias else "")
    return "https://wa.me/" + tel + "?text=" + urllib.parse.quote(texto)


def _clientes_para_exportar(dias_filtro=None):
    data = get_agente_clientes()
    cfg = data["config"]
    sel = [c for c in data["clientes"] if c["seleccionado"]]
    if dias_filtro:
        pedidos = {d.strip().lower() for d in dias_filtro.split(",") if d.strip()}
        sel = [c for c in sel if {d.lower() for d in c["dias_visita"]} & pedidos]
    sel.sort(key=lambda c: (_orden_dia(c["dias_visita"][0]) if c["dias_visita"] else (2, 0, ""),
                            c["nombre"].lower()))
    return sel, cfg


@app.get("/api/agente/export")
def get_agente_export(formato: str = "html", dias: str = None):
    """Exportable de la campaña: un archivo con un renglón por cliente
    seleccionado y el link directo para abrir la conversación de WhatsApp
    con el mensaje ya escrito."""
    sel, cfg = _clientes_para_exportar(dias)
    if not sel:
        raise HTTPException(status_code=400, detail="No hay clientes seleccionados para exportar")
    mensaje = cfg.get("mensaje") or MENSAJE_DEFAULT
    hoy = date.today().strftime("%Y-%m-%d")

    if formato == "csv":
        # Separador ";": es lo que espera Excel en configuración regional es-AR.
        filas = ["Cliente;Persona de contacto;Dia de visita;Telefono;Ultima compra;Link WhatsApp"]
        for c in sel:
            link = _link_whatsapp(c["telefono"], c["nombre"], c["dias_visita"], mensaje, c.get("contacto")) if c["telefono"] else "SIN TELEFONO"
            campos = [c["nombre"], c.get("contacto") or "", " / ".join(c["dias_visita"]), c["telefono_crudo"],
                      c["ultima_compra"] or "", link]
            filas.append(";".join('"' + str(x).replace('"', '""') + '"' for x in campos))
        cuerpo = "﻿" + "\n".join(filas)
        return Response(
            content=cuerpo, media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="whatsapp-{hoy}.csv"'},
        )

    por_dia = defaultdict(list)
    for c in sel:
        por_dia[" / ".join(c["dias_visita"]) or "Sin día asignado"].append(c)

    def esc(t):
        return (str(t or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                .replace('"', "&quot;"))

    bloques = []
    for dia in sorted(por_dia, key=lambda d: _orden_dia(d.split(" / ")[0])):
        filas = []
        for c in por_dia[dia]:
            if c["telefono"]:
                link = _link_whatsapp(c["telefono"], c["nombre"], c["dias_visita"], mensaje, c.get("contacto"))
                accion = f'<a class="wa" href="{esc(link)}" target="_blank" rel="noopener">Abrir WhatsApp</a>'
                tel = esc(c["telefono_crudo"])
            else:
                accion = '<span class="falta">Sin teléfono</span>'
                tel = "—"
            filas.append(
                f'<tr><td><input type="checkbox" class="chk"></td><td>{esc(c["nombre"])}</td>'
                f'<td>{esc(c.get("contacto") or "—")}</td>'
                f'<td class="mono">{tel}</td><td class="mono">{esc(c["ultima_compra"] or "—")}</td>'
                f'<td>{accion}</td></tr>'
            )
        bloques.append(
            f'<h2>{esc(dia)} <small>{len(por_dia[dia])} clientes</small></h2>'
            '<table><thead><tr><th></th><th>Cliente</th><th>Persona de contacto</th>'
            '<th>Teléfono</th><th>Última compra</th><th>WhatsApp</th></tr></thead><tbody>'
            + "".join(filas) + "</tbody></table>"
        )

    sin_tel = sum(1 for c in sel if not c["telefono"])
    html = f"""<!DOCTYPE html>
<html lang="es"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Campaña WhatsApp — {hoy}</title>
<style>
body {{ font-family: -apple-system, 'Segoe UI', Roboto, sans-serif; background:#0b0d11; color:#fff; margin:0; padding:28px; }}
h1 {{ font-size:22px; margin:0 0 4px; }}
.sub {{ color:#8b8f9a; font-size:13px; margin-bottom:22px; }}
.msg {{ background:#16181d; border:1px solid #282c34; border-radius:10px; padding:14px; color:#c9ccd3; font-size:13px; margin-bottom:26px; white-space:pre-wrap; }}
h2 {{ font-size:15px; margin:26px 0 10px; color:#f5c842; }}
h2 small {{ color:#8b8f9a; font-weight:400; font-size:12px; margin-left:8px; }}
table {{ width:100%; border-collapse:collapse; background:#16181d; border:1px solid #282c34; border-radius:10px; overflow:hidden; }}
th, td {{ text-align:left; padding:10px 12px; border-bottom:1px solid #282c34; font-size:14px; }}
th {{ color:#8b8f9a; font-size:11px; text-transform:uppercase; letter-spacing:.08em; }}
tr:last-child td {{ border-bottom:none; }}
tr.hecho td {{ opacity:.4; text-decoration:line-through; }}
.mono {{ font-family:'DM Mono', ui-monospace, monospace; color:#c9ccd3; }}
a.wa {{ background:#25d366; color:#06210f; padding:6px 12px; border-radius:6px; font-weight:600; font-size:13px; text-decoration:none; display:inline-block; }}
.falta {{ color:#ff8c42; font-size:13px; }}
@media print {{ body {{ background:#fff; color:#000; }} table {{ background:#fff; }} a.wa {{ color:#000; }} }}
</style></head><body>
<h1>Campaña WhatsApp · {len(sel)} clientes</h1>
<div class="sub">Generado el {hoy} · {sin_tel} sin teléfono cargado · tildá cada fila a medida que contactás (se guarda en este navegador)</div>
<div class="msg"><b>Mensaje que se abre:</b><br>{esc(mensaje)}</div>
{''.join(bloques)}
<script>
const K='wa-{hoy}';
const st=JSON.parse(localStorage.getItem(K)||'[]');
document.querySelectorAll('tbody tr').forEach((tr,i)=>{{
  const c=tr.querySelector('.chk');
  if(st.includes(i)){{c.checked=true;tr.classList.add('hecho');}}
  c.addEventListener('change',()=>{{
    tr.classList.toggle('hecho',c.checked);
    const s=[...document.querySelectorAll('tbody tr')].map((t,j)=>t.querySelector('.chk').checked?j:-1).filter(j=>j>=0);
    localStorage.setItem(K,JSON.stringify(s));
  }});
}});
</script>
</body></html>"""
    return Response(
        content=html, media_type="text/html; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="whatsapp-{hoy}.html"'},
    )


@app.get("/api/agente/export/preview")
def get_agente_export_preview(dias: str = None):
    """Qué saldría en el exportable, sin bajarlo (para el botón de la UI)."""
    sel, cfg = _clientes_para_exportar(dias)
    return {
        "total": len(sel),
        "con_telefono": sum(1 for c in sel if c["telefono"]),
        "sin_telefono": [c["nombre"] for c in sel if not c["telefono"]],
        "mensaje": cfg.get("mensaje") or MENSAJE_DEFAULT,
    }

# ─── Serve frontend ─────────────────────────────────────────
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/reporte/{vendedor}", response_class=HTMLResponse)
def reporte_vendedor_html(vendedor: str, mes: str = None):
    """Vista standalone de solo lectura: muestra ÚNICAMENTE el avance de
    Objetivos de un vendedor puntual, sin nav ni acceso al resto del
    dashboard. Pensada para el link que se manda por WhatsApp."""
    mes = mes or date.today().strftime("%Y-%m")
    try:
        uid, models = odoo_connect()
        avance = build_objetivos_avance(uid, models, mes)
    except Exception as e:
        return HTMLResponse(f"<html><body style='background:#0b0d11;color:#e8eaf0;font-family:sans-serif;padding:40px'>Error consultando datos: {e}</body></html>", status_code=500)

    avance_vendedor = avance.get(vendedor)
    if not avance_vendedor:
        return HTMLResponse(f"<html><body style='background:#0b0d11;color:#e8eaf0;font-family:sans-serif;padding:40px'>No hay datos para el vendedor '{vendedor}' en {mes}.</body></html>", status_code=404)

    proveedores = sorted(avance_vendedor.keys())

    def barra(pct, label):
        if pct >= 95: color = "#3ecfb2"
        elif pct >= 70: color = "#f5c842"
        else: color = "#ff4f4f"
        clamped = min(100, max(0, pct))
        return f'''<div style="display:flex;align-items:center;gap:8px;min-width:140px">
          <div style="flex:1;background:#1a1e28;border-radius:4px;height:8px;overflow:hidden">
            <div style="height:100%;border-radius:4px;width:{clamped}%;background:{color}"></div>
          </div>
          <span style="font-size:12px;font-weight:700;color:{color};min-width:36px;text-align:right">{round(pct)}%</span>
        </div>'''

    def fmt_money(n): return f"${n:,.0f}".replace(",", ".")
    def fmt_num(n): return f"{n:,.1f}".replace(",", "X").replace(".", ",").replace("X", ".")

    vFactReal = sum(m["facturacion_real"] for m in avance_vendedor.values())
    vFactObj  = sum(m["facturacion_objetivo"] for m in avance_vendedor.values())
    vCajasReal = sum(m["cajas_real"] for m in avance_vendedor.values())
    vCajasObj  = sum(m["cajas_objetivo"] for m in avance_vendedor.values())
    vCartera = next(iter(avance_vendedor.values()))["clientes_cartera"] if avance_vendedor else 0
    vConCompra = max((m["clientes_con_compra"] for m in avance_vendedor.values()), default=0)
    coberturaRealV = round(vConCompra / vCartera * 100, 1) if vCartera > 0 else 0
    objsCob = [m["cobertura_objetivo"] for m in avance_vendedor.values() if m["cobertura_objetivo"] > 0]
    coberturaObjV = sum(objsCob)/len(objsCob) if objsCob else 0

    filas_html = ""
    for p in proveedores:
        m = avance_vendedor[p]
        pct_fact = (m["facturacion_real"]/m["facturacion_objetivo"]*100) if m["facturacion_objetivo"] > 0 else None
        pct_cajas = (m["cajas_real"]/m["cajas_objetivo"]*100) if m["cajas_objetivo"] > 0 else None
        pct_cob = (m["cobertura_real"]/m["cobertura_objetivo"]*100) if m["cobertura_objetivo"] > 0 else None
        filas_html += f'''<tr style="border-bottom:1px solid #252a38">
          <td style="padding:12px 14px;font-size:13px">{p}</td>
          <td style="padding:12px 14px;text-align:right;font-size:13px">{fmt_money(m["facturacion_real"])}<br><span style="color:#5c6278;font-size:11px">obj: {fmt_money(m["facturacion_objetivo"]) if m["facturacion_objetivo"]>0 else "—"}</span></td>
          <td style="padding:12px 14px">{barra(pct_fact, "vtas") if pct_fact is not None else "<span style=color:#5c6278>Sin objetivo</span>"}</td>
          <td style="padding:12px 14px;text-align:right;font-size:13px">{fmt_num(m["cajas_real"])}<br><span style="color:#5c6278;font-size:11px">obj: {fmt_num(m["cajas_objetivo"]) if m["cajas_objetivo"]>0 else "—"}</span></td>
          <td style="padding:12px 14px">{barra(pct_cajas, "cajas") if pct_cajas is not None else "<span style=color:#5c6278>Sin objetivo</span>"}</td>
          <td style="padding:12px 14px;text-align:right;font-size:13px">{m["cobertura_real"]:.1f}% <span style="color:#5c6278">/ obj {m["cobertura_objetivo"]:.1f}%</span></td>
          <td style="padding:12px 14px">{barra(pct_cob, "cob") if pct_cob is not None else "<span style=color:#5c6278>Sin objetivo</span>"}</td>
        </tr>'''

    html = f"""<!DOCTYPE html>
<html lang="es"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Avance Objetivos — {vendedor}</title>
<style>
  body {{ background:#0b0d11; color:#e8eaf0; font-family:'Segoe UI',Arial,sans-serif; margin:0; padding:24px 16px; }}
  h1 {{ font-size:20px; margin:0 0 4px; }}
  .sub {{ color:#5c6278; font-size:13px; margin-bottom:20px; }}
  table {{ width:100%; border-collapse:collapse; font-size:13px; }}
  thead th {{ background:#1a1e28; padding:10px 14px; text-align:left; font-size:10px; color:#5c6278; text-transform:uppercase; letter-spacing:1px; }}
  .tablewrap {{ overflow-x:auto; border-radius:10px; border:1px solid #252a38; }}
  .total-row td {{ background:#1a1e28; border-top:2px solid #3ecfb2; font-weight:800; padding:14px; }}
  .footer {{ margin-top:16px; color:#5c6278; font-size:11px; text-align:center; }}
</style></head>
<body>
  <h1>Avance Objetivos — {vendedor}</h1>
  <div class="sub">{formatMes_py(mes)} · Kairon Distribuciones</div>
  <div class="tablewrap">
  <table>
    <thead><tr>
      <th>Proveedor</th><th>Ventas $</th><th>% Vtas</th><th>Cajas</th><th>% Cajas</th><th>Cobertura</th><th>% Cumpl.</th>
    </tr></thead>
    <tbody>
      <tr class="total-row">
        <td>TOTAL</td>
        <td style="text-align:right">{fmt_money(vFactReal)}<br><span style="color:#5c6278;font-size:11px;font-weight:400">obj: {fmt_money(vFactObj) if vFactObj>0 else "—"}</span></td>
        <td>{barra((vFactReal/vFactObj*100) if vFactObj>0 else 0, "vtas")}</td>
        <td style="text-align:right">{fmt_num(vCajasReal)}<br><span style="color:#5c6278;font-size:11px;font-weight:400">obj: {fmt_num(vCajasObj) if vCajasObj>0 else "—"}</span></td>
        <td>{barra((vCajasReal/vCajasObj*100) if vCajasObj>0 else 0, "cajas")}</td>
        <td style="text-align:right">{coberturaRealV:.1f}% <span style="color:#5c6278">/ obj {coberturaObjV:.1f}%</span></td>
        <td>{barra((coberturaRealV/coberturaObjV*100) if coberturaObjV>0 else 0, "cob")}</td>
      </tr>
      {filas_html}
    </tbody>
  </table>
  </div>
  <div class="footer">Vista de solo lectura · Reporte automático diario</div>
</body></html>"""
    return HTMLResponse(html)

NO_CACHE = {
    "Cache-Control": "no-store, no-cache, must-revalidate",
    "Pragma": "no-cache"
}

# El portal es la home: se entra por aca y desde el menu se navega a cada
# aplicativo. El dashboard vive en /dashboard, que es a donde apunta el
# portal.
@app.get("/")
def root():
    return FileResponse("static/portal.html", headers=NO_CACHE)

@app.get("/dashboard")
def dashboard():
    return FileResponse("static/index.html", headers=NO_CACHE)

@app.get("/agente")
def agente():
    return FileResponse("static/agente.html", headers=NO_CACHE)

# Se mantiene /portal para que no se rompan los links y favoritos que
# quedaron apuntando ahi mientras el portal se probaba.
@app.get("/portal")
def portal():
    return FileResponse("static/portal.html", headers=NO_CACHE)
