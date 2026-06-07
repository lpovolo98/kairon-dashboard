import xmlrpc.client
import os
import time
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

# ─── Data builders ──────────────────────────────────────────

def build_stock_data(uid, models):
    """Stock actual + promedio ventas diario últimos 2 meses + días de inventario"""
    # Stock actual por producto
    quants = odoo_call(models, uid, "stock.quant", "search_read",
        [["location_id.usage", "=", "internal"]],
        ["product_id", "quantity", "reserved_quantity"]
    )
    stock_map = defaultdict(float)
    for q in quants:
        if q["product_id"]:
            pid = q["product_id"][0]
            stock_map[pid] += (q["quantity"] - q.get("reserved_quantity", 0))

    # Ventas últimos 2 meses
    fecha_desde = (date.today() - timedelta(days=60)).strftime("%Y-%m-%d")
    lineas = odoo_call(models, uid, "sale.order.line", "search_read",
        [["order_id.state", "in", ["sale", "done"]],
         ["order_id.date_order", ">=", fecha_desde]],
        ["product_id", "product_uom_qty", "order_id"]
    )
    venta_map = defaultdict(float)
    for l in lineas:
        if l["product_id"]:
            venta_map[l["product_id"][0]] += l["product_uom_qty"]

    # Promedio diario (60 días)
    dias = 60
    avg_map = {pid: qty / dias for pid, qty in venta_map.items()}

    # Info productos
    pids = list(set(list(stock_map.keys()) + list(venta_map.keys())))
    if not pids:
        return []
    productos = models.execute_kw(ODOO_DB, uid, ODOO_PASS,
        "product.product", "search_read",
        [[["id", "in", pids]]],
        {"fields": ["id", "name", "categ_id", "default_code", "uom_id", "standard_price"]}
    )

    result = []
    for p in productos:
        pid = p["id"]
        stock_actual = round(stock_map.get(pid, 0), 2)
        avg_diario   = round(avg_map.get(pid, 0), 3)
        dias_inv     = round(stock_actual / avg_diario, 1) if avg_diario > 0 else 9999

        # Semáforo: rojo < 7 días, amarillo < 21, verde >= 21
        if dias_inv < 7:
            semaforo = "red"
        elif dias_inv < 21:
            semaforo = "yellow"
        else:
            semaforo = "green"

        costo = round(p.get("standard_price", 0) or 0, 2)
        result.append({
            "id": pid,
            "codigo": p.get("default_code") or "",
            "nombre": p["name"],
            "categoria": p["categ_id"][1] if p["categ_id"] else "Sin categoría",
            "uom": p["uom_id"][1] if p["uom_id"] else "",
            "stock_actual": stock_actual,
            "avg_diario": avg_diario,
            "avg_mensual": round(avg_diario * 30, 1),
            "dias_inventario": dias_inv,
            "semaforo": semaforo,
            "costo": costo,
            "valorizado": round(stock_actual * costo, 2),
        })

    result.sort(key=lambda x: x["dias_inventario"])
    return result


def build_ventas_data(uid, models):
    """Informe de ventas: clientes compradores, cajas, recompra — por mes y categoría"""
    fecha_desde = (date.today() - timedelta(days=365)).strftime("%Y-%m-%d")

    ordenes = odoo_call(models, uid, "sale.order", "search_read",
        [["state", "in", ["sale", "done"]], ["date_order", ">=", fecha_desde]],
        ["id", "partner_id", "date_order", "amount_total"]
    )
    order_ids = [o["id"] for o in ordenes]
    order_date = {o["id"]: o["date_order"][:7] for o in ordenes}  # YYYY-MM

    lineas = []
    if order_ids:
        lineas = odoo_call(models, uid, "sale.order.line", "search_read",
            [["order_id", "in", order_ids]],
            ["order_id", "product_id", "product_uom_qty", "price_subtotal"]
        )

    # Obtener categorías de productos
    prod_ids = list({l["product_id"][0] for l in lineas if l["product_id"]})
    prod_categ = {}
    if prod_ids:
        prods = models.execute_kw(ODOO_DB, uid, ODOO_PASS,
            "product.product", "search_read",
            [[["id", "in", prod_ids]]],
            {"fields": ["id", "categ_id"]}
        )
        prod_categ = {p["id"]: (p["categ_id"][1] if p["categ_id"] else "Sin categoría") for p in prods}

    # Agregar por mes
    meses = defaultdict(lambda: {
        "clientes": set(), "cajas": 0, "monto": 0,
        "ordenes_por_cliente": defaultdict(int),
        "categorias": defaultdict(float)
    })

    for l in lineas:
        oid = l["order_id"][0] if l["order_id"] else None
        if not oid or oid not in order_date:
            continue
        mes = order_date[oid]
        orden = next((o for o in ordenes if o["id"] == oid), None)
        if not orden:
            continue
        partner_id = orden["partner_id"][0] if orden["partner_id"] else None
        categ = prod_categ.get(l["product_id"][0], "Sin categoría") if l["product_id"] else "Sin categoría"

        meses[mes]["clientes"].add(partner_id)
        meses[mes]["cajas"] += l["product_uom_qty"]
        meses[mes]["monto"] += l["price_subtotal"]
        meses[mes]["ordenes_por_cliente"][partner_id] += 1
        meses[mes]["categorias"][categ] += l["product_uom_qty"]

    result = []
    for mes, data in sorted(meses.items()):
        recompra = sum(1 for cnt in data["ordenes_por_cliente"].values() if cnt > 1)
        result.append({
            "mes": mes,
            "clientes_compradores": len(data["clientes"]),
            "cajas_vendidas": round(data["cajas"], 1),
            "monto_total": round(data["monto"], 2),
            "clientes_recompra": recompra,
            "categorias": dict(data["categorias"]),
        })

    return result


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
            ["order_id", "product_id", "product_uom_qty", "price_subtotal"]
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

@app.get("/api/status")
def status():
    return {
        "ok": True,
        "odoo_url": ODOO_URL,
        "cache_ttl": CACHE_TTL,
        "cached_keys": list(_cache.keys()),
        "timestamp": datetime.now().isoformat()
    }

@app.get("/api/refresh")
def refresh_all():
    uid, models = odoo_connect()
    cache_set("stock",    build_stock_data(uid, models))
    cache_set("ventas",   build_ventas_data(uid, models))
    cache_set("clientes", build_clientes_data(uid, models))
    return {"ok": True, "refreshed_at": datetime.now().isoformat()}

# ─── Serve frontend ─────────────────────────────────────────
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
def root():
    return FileResponse("static/index.html", headers={
        "Cache-Control": "no-store, no-cache, must-revalidate",
        "Pragma": "no-cache"
    })
