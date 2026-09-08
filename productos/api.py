"""Rutas del agente de productos.

Reutiliza la identidad que el middleware del portal ya verificó contra
Cloudflare Access: acá no hay login propio ni token nuevo.
"""

import json
import os
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse

from . import cargar, imagenes
from .esquema import HOJAS

router = APIRouter()
RAIZ = Path(__file__).resolve().parents[1]

# Tope de seguridad: por encima de esto la corrida se rechaza. Se puede subir
# por variable de entorno, pero el default protege de un pegado accidental.
TOPE_REGISTROS = int(os.getenv("PRODUCTOS_TOPE", "300"))


def directorio():
    p = Path(os.getenv("PRODUCTOS_DATA_DIR",
                       "/data/productos" if Path("/data").is_dir() else str(RAIZ / "productos-data")))
    p.mkdir(parents=True, exist_ok=True)
    return p


@contextmanager
def conectar():
    db = sqlite3.connect(directorio() / "corridas.sqlite", timeout=15)
    try:
        with db:
            db.execute("CREATE TABLE IF NOT EXISTS corridas ("
                       "id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL, data TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS pendientes ("
                       "id TEXT PRIMARY KEY, data TEXT NOT NULL)")
            yield db
    finally:
        db.close()


def autorizar(request: Request):
    """La identidad la pone el middleware del portal después de verificar la
    firma de Cloudflare. Un header suelto no alcanza."""
    identidad = getattr(request.state, "usuario", None)
    if not (identidad and identidad.get("email")):
        raise HTTPException(401, "Ingresá por app.kaironsrl.com.ar con una cuenta habilitada.")
    email = identidad["email"].strip().lower()
    permitidos = [v.strip().lower() for v in os.getenv("PRODUCTOS_ALLOWED_EMAILS", "").split(",") if v.strip()]
    if permitidos and email not in permitidos:
        raise HTTPException(403, "Tu cuenta no está habilitada para el ABM de productos.")
    return email


def _odoo():
    from administracion.odoo import Odoo, OdooError
    try:
        return Odoo.desde_config()
    except OdooError as e:
        raise HTTPException(503, str(e))


def _limpiar(plan):
    """Las claves internas (_escribir, _id) no salen al navegador: el cliente
    manda filas, nunca escrituras."""
    return {hoja: [{k: v for k, v in f.items() if not k.startswith("_")} for f in filas]
            for hoja, filas in plan.items()}


# ─── Pantalla y datos de apoyo ───────────────────────────────

@router.get("/productos")
def pagina():
    return FileResponse(RAIZ / "static" / "productos.html")


@router.get("/api/productos/status")
def status(owner=Depends(autorizar)):
    faltan = [k for k in ("ODOO_URL", "ODOO_DB", "ODOO_USER") if not os.getenv(k)]
    if not (os.getenv("ODOO_KEY") or os.getenv("ODOO_PASSWORD")):
        faltan.append("ODOO_PASSWORD")
    return {"ready": not faltan, "missing": faltan, "tope": TOPE_REGISTROS}


@router.get("/api/productos/catalogo")
def catalogo(owner=Depends(autorizar)):
    """Los valores válidos de cada desplegable, tal como están hoy en Odoo.
    Es lo que hacía descubrir_catalogo.py, servido a la pantalla."""
    o = _odoo()
    def leer(modelo, dominio=None, limite=400):
        filas = o.call(modelo, "search_read", [dominio or []],
                       {"fields": ["display_name"], "limit": limite, "order": "display_name"})
        return [{"id": f["id"], "name": f["display_name"]} for f in filas]
    return {
        "categorias":       leer("product.category"),
        "unidades":         leer("uom.uom"),
        "impuestos_venta":  leer("account.tax", [["type_tax_use", "=", "sale"]]),
        "impuestos_compra": leer("account.tax", [["type_tax_use", "=", "purchase"]]),
        "listas":           leer("product.pricelist"),
        "proveedores":      leer("res.partner", [["supplier_rank", ">", 0]]),
    }


@router.get("/api/productos/plantilla")
def plantilla(owner=Depends(autorizar)):
    """Encabezados de cada hoja, para armar la plantilla del lado del cliente."""
    return {hoja: [c["col"] for c in conf["columnas"]] for hoja, conf in HOJAS.items()}


@router.get("/api/productos/sin-imagen")
def productos_sin_imagen(owner=Depends(autorizar)):
    return imagenes.sin_imagen(_odoo())


# ─── Previsualizar y aplicar ─────────────────────────────────

@router.post("/api/productos/previsualizar")
def previsualizar(cuerpo: dict, owner=Depends(autorizar)):
    hojas = _hojas_de(cuerpo)
    try:
        return _limpiar(cargar.previsualizar(_odoo(), hojas))
    except cargar.Frenar as e:
        raise HTTPException(422, str(e))


@router.post("/api/productos/aplicar")
def aplicar(cuerpo: dict, owner=Depends(autorizar)):
    hojas = _hojas_de(cuerpo)
    o = _odoo()
    # Un solo escritor por vez: dos corridas en paralelo sobre el mismo SKU
    # crearían el producto dos veces.
    with closing(sqlite3.connect(directorio() / "escritor.sqlite", timeout=600)) as guard, guard:
        guard.execute("CREATE TABLE IF NOT EXISTS mutex (id INTEGER)")
        guard.execute("BEGIN IMMEDIATE")
        try:
            r = cargar.aplicar(o, hojas, tope=TOPE_REGISTROS)
        except cargar.Frenar as e:
            raise HTTPException(422, str(e))
    return _guardar_corrida(owner, "catalogo", r)


@router.post("/api/productos/imagenes")
def subir_imagenes(cuerpo: dict, owner=Depends(autorizar)):
    asignaciones = cuerpo.get("asignaciones") or []
    if not isinstance(asignaciones, list) or not asignaciones:
        raise HTTPException(422, "No llegó ninguna imagen.")
    if len(asignaciones) > TOPE_REGISTROS:
        raise HTTPException(422, f"Son {len(asignaciones)} imágenes y el tope es {TOPE_REGISTROS}.")
    return _guardar_corrida(owner, "imagenes", imagenes.aplicar(_odoo(), asignaciones))


@router.get("/api/productos/corridas")
def corridas(owner=Depends(autorizar)):
    with conectar() as db:
        filas = db.execute("SELECT id,data FROM corridas WHERE owner=? ORDER BY id DESC LIMIT 20",
                           (owner,)).fetchall()
    return [dict(json.loads(d), id=i) for i, d in filas]


@router.post("/api/productos/corridas/{corrida_id}/revertir")
def revertir(corrida_id: int, owner=Depends(autorizar)):
    with conectar() as db:
        fila = db.execute("SELECT data FROM corridas WHERE id=? AND owner=?",
                          (corrida_id, owner)).fetchone()
    if not fila:
        raise HTTPException(404, "Esa corrida no existe.")
    datos = json.loads(fila[0])
    if datos.get("revertida"):
        raise HTTPException(409, "Esa corrida ya fue revertida.")
    r = cargar.revertir(_odoo(), datos.get("deshacer") or {})
    datos["revertida"] = datetime.now(timezone.utc).isoformat()
    with conectar() as db:
        db.execute("UPDATE corridas SET data=? WHERE id=?", (json.dumps(datos), corrida_id))
    return r


# ─── Cola compartida con el agente administrativo ────────────

@router.get("/api/productos/pendientes")
def pendientes(owner=Depends(autorizar)):
    with conectar() as db:
        filas = db.execute("SELECT data FROM pendientes").fetchall()
    return [json.loads(d) for (d,) in filas
            if json.loads(d).get("estado", "pendiente") == "pendiente"]


def encolar(item):
    """La llama el agente administrativo cuando una factura trae un código de
    proveedor que todavía no existe como producto. Idempotente por
    proveedor + código: una factura reintentada no duplica el pedido."""
    clave = f"{item.get('proveedor','')}|{item.get('codigo_proveedor','')}"
    item = dict(item, estado=item.get("estado", "pendiente"),
                creado=datetime.now(timezone.utc).isoformat())
    with conectar() as db:
        db.execute("INSERT OR IGNORE INTO pendientes VALUES (?,?)", (clave, json.dumps(item)))
    return clave


# ─── Auxiliares ──────────────────────────────────────────────

def _hojas_de(cuerpo):
    if not isinstance(cuerpo, dict):
        raise HTTPException(422, "Cuerpo inválido.")
    hojas = {h: cuerpo.get(h) or [] for h in HOJAS}
    if not any(hojas.values()):
        raise HTTPException(422, "No llegó ninguna fila.")
    for nombre, filas in hojas.items():
        if not isinstance(filas, list) or any(not isinstance(f, dict) for f in filas):
            raise HTTPException(422, f"La hoja «{nombre}» tiene que ser una lista de filas.")
    return hojas


def _guardar_corrida(owner, tipo, resultado):
    datos = dict(resultado, tipo=tipo, cuando=datetime.now(timezone.utc).isoformat())
    with conectar() as db:
        cur = db.execute("INSERT INTO corridas (owner,data) VALUES (?,?)", (owner, json.dumps(datos)))
        datos["id"] = cur.lastrowid
    return datos
