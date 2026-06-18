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

# ─── Data builders ──────────────────────────────────────────

def build_stock_data(uid, models):
    """Stock actual en CAJAS + promedio de ventas (facturado) en CAJAS + días de inventario.
    Todo el módulo trabaja en cajas: stock físico ÷ unid_caja, venta facturada normalizada ÷ unid_caja."""
    uom_factors = get_uom_factors(models, uid)

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
        {"fields": ["id", "name", "categ_id", "default_code", "uom_id", "standard_price", "x_studio_unidades_por_caja"]}
    )

    dias = 60
    result = []
    for p in productos:
        pid = p["id"]
        unid_caja = p.get("x_studio_unidades_por_caja") or 1

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

    # Info de productos: categoría, costo, unidades por caja (para mostrar en "cajas")
    prod_ids = list({l["product_id"][0] for l in lineas if l["product_id"]})
    prod_info = {}
    if prod_ids:
        prods = models.execute_kw(ODOO_DB, uid, ODOO_PASS,
            "product.product", "search_read",
            [[["id", "in", prod_ids]]],
            {"fields": ["id", "name", "categ_id", "default_code", "x_studio_unidades_por_caja"]}
        )
        for p in prods:
            prod_info[p["id"]] = {
                "nombre":   p["name"],
                "categoria": p["categ_id"][1] if p["categ_id"] else "Sin categoría",
                "codigo":   p.get("default_code") or "",
                "unid_caja": p.get("x_studio_unidades_por_caja") or 1,
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
        ["id", "partner_id", "date_order", "amount_total"]
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
            oportunidad = f"No compró este mes — último pedido hace {(date.today() - date.fromisoformat(ords[-1]['date_order'][:10])).days} días"
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
        })

    result.sort(key=lambda x: (-x["monto_90d"]))
    return result



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

@app.get("/api/status")
def status():
    return {
        "ok": True,
        "odoo_url": ODOO_URL,
        "cache_ttl": CACHE_TTL,
        "cached_keys": list(_cache.keys()),
        "timestamp": datetime.now().isoformat()
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
    cache_set("stock",      build_stock_data(uid, models))
    cache_set("ventas",     build_ventas_data(uid, models))
    cache_set("clientes",   build_clientes_data(uid, models))
    cache_set("cartera",    build_cartera_data(uid, models))
    cache_set("cobranzas",  build_cobranzas_data(uid, models))
    return {"ok": True, "refreshed_at": datetime.now().isoformat()}

# ─── Serve frontend ─────────────────────────────────────────
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
def root():
    return FileResponse("static/index.html", headers={
        "Cache-Control": "no-store, no-cache, must-revalidate",
        "Pragma": "no-cache"
    })
