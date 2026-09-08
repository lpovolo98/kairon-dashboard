"""Motor del ABM de productos.

Porta la lógica probada de cargar_catalogo.py —resolución de nombres a ids,
búsqueda de SKU tolerante a espacios, desambiguación de impuestos por
alcance— y le agrega lo que ese script no hacía: comparar contra el producto
existente para separar altas de modificaciones, y no escribir las filas que
no cambian.

Garantía estructural: previsualizar recibe un Odoo envuelto en SoloLectura,
que revienta si alguien intenta escribir. No depende de la disciplina de
quien toque el código después.
"""

import re
import unicodedata

from .esquema import (COLUMNAS_PRODUCTOS, FIJOS_AL_CREAR, HOJAS, columna_clave)

_PATRON_ID = re.compile(r"\(id\s*(\d+)\)\s*$")
ESCRITURAS = {"create", "write", "unlink", "copy", "action_archive", "action_unarchive"}


class Frenar(ValueError):
    """Algo que impide seguir y que el usuario tiene que resolver."""


class SoloLectura:
    """Envuelve un Odoo y deja pasar solo lecturas.

    Cualquier intento de escribir durante una previsualización es un error de
    programación, no un dato mal cargado: por eso levanta en vez de avisar.
    """

    def __init__(self, odoo):
        self._odoo = odoo

    def call(self, modelo, metodo, args, kwargs=None):
        if metodo in ESCRITURAS:
            raise AssertionError(
                f"Se intentó {modelo}.{metodo} durante una previsualización. "
                f"La previsualización nunca escribe.")
        return self._odoo.call(modelo, metodo, args, kwargs)

    def __getattr__(self, nombre):
        return getattr(self._odoo, nombre)


# ─── Normalización de valores ────────────────────────────────

def sin_acentos(s):
    return unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower()


def extraer_id_opcional(texto):
    """'Caja (id 42)' -> ('Caja', 42). 'Papas Fritas' -> ('Papas Fritas', None)."""
    if texto is None or texto == "":
        return None, None
    texto = str(texto).strip()
    m = _PATRON_ID.search(texto)
    if not m:
        return texto, None
    return texto[: m.start()].strip(), int(m.group(1))


def a_numero(v):
    """Excel manda números como texto, con coma o con punto, y a veces vacío."""
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    t = str(v).strip().replace(".", "").replace(",", ".") if _con_separador_miles(v) else str(v).strip().replace(",", ".")
    try:
        return float(t)
    except ValueError:
        return None


def _con_separador_miles(v):
    # "1.740,50" tiene punto de miles; "1740.5" no. Se distingue por la coma.
    t = str(v)
    return "," in t and "." in t and t.rfind(",") > t.rfind(".")


def a_booleano(v):
    if isinstance(v, bool):
        return v
    return sin_acentos(v).strip() in ("si", "s", "true", "1", "x", "verdadero")


def mismo_valor(a, b, tipo):
    """Compara como los compararía una persona: 1740 == '1740' == 1740.0, y
    'Caja ' == 'caja'. Sin esto toda fila parece modificada y se reescribe
    Odoo entero sin necesidad."""
    if tipo == "numero":
        na, nb = a_numero(a), a_numero(b)
        if na is None and nb is None:
            return True
        if na is None or nb is None:
            return False
        return abs(na - nb) < 1e-6
    if tipo == "booleano":
        return bool(a) == bool(b)
    if tipo == "m2m":
        return sorted(a or []) == sorted(b or [])
    if tipo == "m2o":
        return (a or None) == (b or None)
    return str(a or "").strip() == str(b or "").strip()


def formatear(v, tipo):
    """Cómo se muestra el valor en la pantalla."""
    if v is None or v == "":
        return ""
    if tipo == "numero":
        n = a_numero(v)
        if n is None:
            return str(v)
        return int(n) if float(n).is_integer() else round(n, 4)
    if tipo == "booleano":
        return "Sí" if v else "No"
    if tipo == "m2m":
        return ", ".join(v) if isinstance(v, list) else str(v)
    return str(v).strip()


# ─── Resolución de nombres a ids ─────────────────────────────

class Resolvedor:
    """Busca ids por nombre (o por '(id N)' explícito) y cachea.

    Junta los errores en vez de cortar en el primero, para que el usuario vea
    todos los problemas juntos en una sola pasada.
    """

    def __init__(self, odoo, lang="es_AR"):
        self.odoo = odoo
        self.lang = lang
        self.cache = {}

    def buscar(self, modelo, texto, errores, dominio_extra=None, etiqueta=None):
        nombre, id_forzado = extraer_id_opcional(texto)
        if not nombre:
            return None
        # La caché distingue el alcance: si no, "IVA 21%" resuelto para venta
        # queda pisando la búsqueda de compra y asigna el impuesto equivocado
        # sin ningún error visible.
        alcance = tuple(tuple(x) for x in (dominio_extra or []))
        clave = (modelo, str(texto), alcance)
        if clave in self.cache:
            return self.cache[clave]

        campo = etiqueta or modelo

        if id_forzado is not None:
            existe = self.odoo.call(modelo, "search", [[["id", "=", id_forzado]]])
            if not existe:
                errores.append(f"El id {id_forzado} indicado para «{nombre}» en {campo} no existe en Odoo.")
                self.cache[clave] = None
                return None
            self.cache[clave] = id_forzado
            return id_forzado

        dominio = [["name", "=", nombre]] + (dominio_extra or [])
        ids = self.odoo.call(modelo, "search", [dominio], {"context": {"lang": self.lang}})

        if not ids:
            parecidos = self.odoo.call(
                modelo, "search", [[["name", "ilike", nombre]] + (dominio_extra or [])],
                {"limit": 5, "context": {"lang": self.lang}})
            if parecidos:
                regs = self.odoo.call(modelo, "read", [parecidos],
                                      {"fields": ["display_name"], "context": {"lang": self.lang}})
                sugeridos = ", ".join(r["display_name"] for r in regs)
                errores.append(f"No existe «{nombre}» en {campo}. ¿Quisiste decir: {sugeridos}?")
            else:
                errores.append(f"No existe «{nombre}» en {campo}.")
            self.cache[clave] = None
            return None

        if len(ids) > 1:
            errores.append(
                f"«{nombre}» en {campo} es ambiguo: hay {len(ids)} registros con ese nombre. "
                f"Escribilo como «{nombre} (id N)» para indicar cuál.")
            self.cache[clave] = None
            return None

        self.cache[clave] = ids[0]
        return ids[0]

    def impuestos(self, texto, alcance, errores, etiqueta):
        if not texto:
            return []
        ids = []
        for nombre in [n.strip() for n in str(texto).split(",") if n.strip()]:
            i = self.buscar("account.tax", nombre, errores,
                            dominio_extra=[["type_tax_use", "=", alcance]], etiqueta=etiqueta)
            if i:
                ids.append(i)
        return ids


def buscar_producto_por_sku(odoo, sku, errores=None):
    """Busca product.template por Referencia interna tolerando espacios de más.

    Es un problema real y ya visto en esta base: hay códigos cargados en Odoo
    con un espacio pegado. Sin esto, una búsqueda exacta no encuentra el
    producto existente y se termina creando un duplicado con el mismo código
    "a la vista".
    """
    sku_norm = str(sku).strip().lower()
    candidatos = odoo.call("product.template", "search", [[["default_code", "ilike", sku]]])
    if not candidatos:
        return None
    regs = odoo.call("product.template", "read", [candidatos], {"fields": ["default_code"]})
    coincidencias = [r["id"] for r in regs if (r.get("default_code") or "").strip().lower() == sku_norm]
    if len(coincidencias) > 1:
        if errores is not None:
            errores.append(
                f"Hay {len(coincidencias)} productos en Odoo cuya Referencia interna coincide con "
                f"«{sku}» salvo espacios. Corregí el código directamente en Odoo antes de continuar.")
        return None
    return coincidencias[0] if coincidencias else None


# ─── Previsualización ────────────────────────────────────────

def _leer_existente(odoo, modelo, id_, columnas):
    campos = [c["campo"] for c in columnas if not c["campo"].startswith("_")]
    campos = odoo.solo_existentes(modelo, campos) if hasattr(odoo, "solo_existentes") else campos
    regs = odoo.call(modelo, "read", [[id_]], {"fields": campos})
    return regs[0] if regs else {}


def _nombre_de(odoo, modelo, id_, cache):
    """Un m2o guardado en Odoo se lee como [id, nombre]; cuando no, se busca."""
    if not id_:
        return ""
    clave = (modelo, id_)
    if clave not in cache:
        r = odoo.call(modelo, "read", [[id_]], {"fields": ["display_name"]})
        cache[clave] = r[0]["display_name"] if r else str(id_)
    return cache[clave]


def _valor_actual(reg, spec, odoo, cache):
    """Traduce lo que devuelve Odoo al mismo formato que el valor deseado."""
    bruto = reg.get(spec["campo"])
    if spec["tipo"] == "m2o":
        if isinstance(bruto, (list, tuple)) and bruto:
            return bruto[0], bruto[1]
        return None, ""
    if spec["tipo"] == "m2m":
        ids = list(bruto or [])
        return ids, ", ".join(_nombre_de(odoo, spec["modelo"], i, cache) for i in ids)
    if spec["tipo"] == "numero":
        return bruto, formatear(bruto, "numero")
    if spec["tipo"] == "booleano":
        return bool(bruto), formatear(bool(bruto), "booleano")
    return bruto, formatear(bruto, "texto")


def _valor_deseado(spec, crudo, res, errores):
    """Devuelve (valor_para_odoo, texto_para_mostrar)."""
    tipo = spec["tipo"]
    if tipo == "m2o":
        id_ = res.buscar(spec["modelo"], crudo, errores, etiqueta=spec.get("etiqueta"))
        nombre, _ = extraer_id_opcional(crudo)
        return id_, (nombre or "")
    if tipo == "m2m":
        ids = res.impuestos(crudo, spec["alcance"], errores, spec.get("etiqueta"))
        return ids, ", ".join(n.strip() for n in str(crudo or "").split(",") if n.strip())
    if tipo == "numero":
        n = a_numero(crudo)
        return n, formatear(n, "numero")
    if tipo == "booleano":
        b = a_booleano(crudo)
        return b, formatear(b, "booleano")
    return (str(crudo).strip() if crudo not in (None, "") else ""), formatear(crudo, "texto")


def previsualizar_hoja(odoo, hoja, filas, res=None, skus_validos=None, skus_futuros=None):
    """Cruza cada fila contra Odoo y decide si es alta, modificación,
    idéntica o error. No escribe nada: el odoo que recibe es de solo lectura."""
    conf = HOJAS[hoja]
    columnas = conf["columnas"]
    clave = columna_clave(hoja)
    res = res or Resolvedor(odoo)
    cache_nombres = {}
    vistos = {}
    salida = []

    for i, cruda in enumerate(filas, start=2):
        errores = []
        sku = str(cruda.get(clave) or "").strip()
        if not sku:
            continue

        if sku in vistos:
            errores.append(f"El SKU «{sku}» aparece más de una vez en el archivo (filas {vistos[sku]} y {i}).")
        vistos.setdefault(sku, i)

        if skus_validos is not None and sku not in skus_validos:
            errores.append(f"El SKU «{sku}» no está en la hoja Productos.")

        tmpl_id = buscar_producto_por_sku(odoo, sku, errores)
        destino_id, existente = _resolver_destino(
            odoo, hoja, tmpl_id, sku, cruda, res, errores, skus_futuros or set())

        actual = _leer_existente(odoo, conf["modelo"], destino_id, columnas) if destino_id else {}
        campos, escribir, hay_cambio = {}, {}, False

        for spec in columnas:
            if spec.get("clave"):
                continue
            crudo = cruda.get(spec["col"])
            presente = spec["col"] in cruda
            # Una columna ausente en una fila que modifica algo existente
            # significa "no lo toques", no "lo estas borrando". Exigirla solo
            # tiene sentido al crear, o si vino explicitamente vacia.
            if spec.get("requerido") and (crudo in (None, "")) and (not destino_id or presente):
                errores.append(f"Falta «{spec['col']}».")
            if spec.get("aviso_si_falta") and (crudo in (None, "")) and not destino_id:
                errores.append(f"Falta «{spec['col']}». {spec['aviso_si_falta']}")
            if not presente:
                continue

            valor, texto = _valor_deseado(spec, crudo, res, errores)
            dato = {"nuevo": texto}
            if spec.get("opciones"):
                dato["opciones"] = spec["opciones"]

            if destino_id:
                val_actual, txt_actual = _valor_actual(actual, spec, odoo, cache_nombres)
                if not mismo_valor(valor, val_actual, spec["tipo"]):
                    dato["viejo"] = txt_actual
                    escribir[spec["campo"]] = _para_odoo(spec, valor)
                    hay_cambio = True
            else:
                if valor not in (None, "", []):
                    escribir[spec["campo"]] = _para_odoo(spec, valor)
            campos[spec["col"]] = dato

        if errores:
            estado = "error"
        elif not destino_id:
            estado = "alta"
        elif hay_cambio:
            estado = "modificacion"
        else:
            estado = "igual"

        salida.append({"sku": sku, "estado": estado, "errores": errores, "campos": campos,
                       "_id": destino_id, "_tmpl": tmpl_id, "_escribir": escribir,
                       "_existente": existente})
    return salida


def _para_odoo(spec, valor):
    # Los m2m en XML-RPC se escriben con el comando (6, 0, ids).
    return [(6, 0, valor)] if spec["tipo"] == "m2m" else valor


def _resolver_destino(odoo, hoja, tmpl_id, sku, cruda, res, errores, skus_futuros):
    """Qué registro se está por tocar. En productos es el producto mismo; en
    las otras dos hojas, la línea de proveedor o la regla de precio."""
    if hoja == "productos":
        return tmpl_id, bool(tmpl_id)
    if not tmpl_id:
        # Si el producto se da de alta en esta misma corrida todavía no existe
        # en Odoo, pero va a existir cuando le toque el turno a esta hoja: los
        # productos se escriben primero.
        if sku not in skus_futuros:
            errores.append("El producto todavía no existe en Odoo. Cargalo primero en la hoja Productos.")
        return None, False

    if hoja == "proveedores":
        partner_id = res.buscar("res.partner", cruda.get("Proveedor"), errores, etiqueta="Proveedor")
        if not partner_id:
            return None, False
        ids = odoo.call("product.supplierinfo", "search",
                        [[["product_tmpl_id", "=", tmpl_id], ["partner_id", "=", partner_id]]])
        return (ids[0] if ids else None), bool(ids)

    lista_id = res.buscar("product.pricelist", cruda.get("Lista de precios"), errores,
                          etiqueta="Lista de precios")
    if not lista_id:
        return None, False
    ids = odoo.call("product.pricelist.item", "search",
                    [[["pricelist_id", "=", lista_id], ["product_tmpl_id", "=", tmpl_id],
                      ["applied_on", "=", "1_product"]]])
    return (ids[0] if ids else None), bool(ids)


def previsualizar(odoo, hojas):
    """hojas: {'productos': [...], 'proveedores': [...], 'precios': [...]}"""
    lector = SoloLectura(odoo)
    res = Resolvedor(lector)
    productos = previsualizar_hoja(lector, "productos", hojas.get("productos") or [], res)
    skus = {f["sku"] for f in productos}
    futuros = {f["sku"] for f in productos if f["estado"] == "alta"}
    return {
        "productos": productos,
        "proveedores": previsualizar_hoja(lector, "proveedores", hojas.get("proveedores") or [],
                                          res, skus or None, futuros),
        "precios":    previsualizar_hoja(lector, "precios", hojas.get("precios") or [],
                                         res, skus or None, futuros),
    }


# ─── Escritura ───────────────────────────────────────────────

def aplicar(odoo, hojas, tope=None):
    """Vuelve a previsualizar del lado del servidor —nunca confía en el plan
    que manda el navegador— y recién ahí escribe. Devuelve el resultado y lo
    necesario para revertir la corrida."""
    plan = previsualizar(odoo, hojas)

    con_error = [f for h in plan.values() for f in h if f["estado"] == "error"]
    if con_error:
        raise Frenar(f"Hay {len(con_error)} fila(s) con error. No se escribió nada.")

    a_escribir = [(h, f) for h, filas in plan.items() for f in filas
                  if f["estado"] in ("alta", "modificacion")]
    if tope and len(a_escribir) > tope:
        raise Frenar(f"La corrida toca {len(a_escribir)} registros y el tope configurado es {tope}.")

    creados, modificados, deshacer = [], [], {"creados": [], "modificados": []}

    # Los productos van primero: las otras dos hojas cuelgan de ellos.
    for hoja in ("productos", "proveedores", "precios"):
        modelo = HOJAS[hoja]["modelo"]
        for fila in plan[hoja]:
            if fila["estado"] not in ("alta", "modificacion"):
                continue
            valores = dict(fila["_escribir"])

            if fila["estado"] == "modificacion":
                previos = _leer_existente(odoo, modelo, fila["_id"], HOJAS[hoja]["columnas"])
                odoo.call(modelo, "write", [[fila["_id"]], valores])
                deshacer["modificados"].append({
                    "modelo": modelo, "id": fila["_id"],
                    "valores": {c: _revertible(previos.get(c)) for c in valores},
                })
                modificados.append({"hoja": hoja, "sku": fila["sku"], "id": fila["_id"]})
                continue

            if hoja == "productos":
                valores.update(FIJOS_AL_CREAR)
                valores["default_code"] = fila["sku"]
            else:
                # La fila necesita el producto: puede haberse creado recién.
                tmpl = fila["_tmpl"] or buscar_producto_por_sku(odoo, fila["sku"])
                if not tmpl:
                    continue
                valores["product_tmpl_id"] = tmpl
                if hoja == "precios":
                    valores.update({"applied_on": "1_product", "compute_price": "fixed"})

            nuevo_id = odoo.call(modelo, "create", [valores])
            deshacer["creados"].append({"modelo": modelo, "id": nuevo_id})
            creados.append({"hoja": hoja, "sku": fila["sku"], "id": nuevo_id})

    return {"creados": creados, "modificados": modificados, "deshacer": deshacer}


def _revertible(v):
    """Un m2o leído viene como [id, nombre]; para reescribirlo hay que mandar
    solo el id. Un m2m, como lista de ids, va con el comando (6, 0, ids)."""
    if isinstance(v, (list, tuple)):
        if len(v) == 2 and isinstance(v[0], int) and isinstance(v[1], str):
            return v[0]
        return [(6, 0, list(v))]
    return v


def revertir(odoo, deshacer):
    """Deshace una corrida: devuelve los campos modificados a su valor previo
    y archiva lo creado. No borra: un producto con movimientos no se puede
    borrar, y archivar es reversible."""
    devueltos, archivados = 0, 0
    for m in deshacer.get("modificados", []):
        odoo.call(m["modelo"], "write", [[m["id"]], m["valores"]])
        devueltos += 1
    for c in deshacer.get("creados", []):
        odoo.call(c["modelo"], "write", [[c["id"]], {"active": False}])
        archivados += 1
    return {"devueltos": devueltos, "archivados": archivados}
