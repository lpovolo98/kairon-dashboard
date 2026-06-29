import xmlrpc.client
import os
import time
import urllib.request
import json
from datetime import datetime, date, timedelta
from collections import defaultdict
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from pydantic import BaseModel
from PIL import Image, ImageDraw, ImageFont
from twilio.rest import Client as TwilioClient
import threading

load_dotenv()

app = FastAPI(title="Odoo Dashboard API")
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

    # Info productos
    pids = list(set(list(stock_map.keys()) + list(venta_map.keys())))
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

# ─── Reporte diario por WhatsApp (imagen del avance de Objetivos) ────
def _semaforo_color(pct):
    if pct >= 95: return (62, 207, 178)   # verde (--green)
    if pct >= 70: return (245, 200, 66)   # amarillo (--yellow)
    return (255, 79, 79)                  # rojo (--red)

def _fmt_money(n):
    return f"${n:,.0f}".replace(",", ".")

def _fmt_num(n):
    return f"{n:,.1f}".replace(",", "X").replace(".", ",").replace("X", ".")

def _cargar_fuente(size, bold=False):
    """Intenta usar DejaVuSans (suele venir instalada en la imagen base de
    Python/Debian); si no está disponible, cae al font default de Pillow
    para que la generación nunca falle por falta de fuente."""
    candidatos = [
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
        client.messages.create(
            from_=TWILIO_WHATSAPP_FROM,
            to=REPORTE_WHATSAPP_TO,
            body=f"📊 Avance de objetivos — {vendedor} — {formatMes_py(mes)}",
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

# ─── Serve frontend ─────────────────────────────────────────
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
def root():
    return FileResponse("static/index.html", headers={
        "Cache-Control": "no-store, no-cache, must-revalidate",
        "Pragma": "no-cache"
    })
