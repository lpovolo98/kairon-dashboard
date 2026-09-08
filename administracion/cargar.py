#!/usr/bin/env python3
"""Carga una factura de proveedor en Odoo: orden de compra + factura, en borrador.

Recibe el JSON estructurado de la factura (el que arma el agente leyendo el PDF)
y hace el trabajo determinístico: resolver proveedor, mapear productos, convertir
unidades, elegir diario, armar impuestos, controlar precios y escribir.

Por defecto **simula**. Para escribir de verdad hay que pasar `--escribir`.

    python3 cargar.py factura.json                  # simulación + informe
    python3 cargar.py factura.json --escribir       # crea OC y factura en borrador
    python3 cargar.py factura.json --escribir --confirmar-oc

Sobre `--confirmar-oc`: en Odoo el ingreso de mercadería (`stock.picking`) no
existe hasta que la orden de compra se confirma, y la vinculación real entre
factura y OC tampoco. Sin ese flag queda todo en borrador — OC como solicitud de
presupuesto, factura en borrador, sin picking — y la vinculación queda anotada en
la referencia. Con el flag, la OC se confirma (aparece el ingreso, sin validar) y
la factura se crea vinculada línea por línea. En ninguno de los dos casos se
valida el ingreso ni se contabiliza la factura.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from .odoo import Odoo, OdooError

RAIZ = Path(__file__).resolve().parent

# Tolerancias de reconciliación aritmética contra el PDF.
TOL_LINEA_ABS = 0.50      # los proveedores redondean distinto en cada línea
TOL_LINEA_PCT = 0.001     # 0,1%
TOL_TOTAL_ABS = 1.00


class Frenar(Exception):
    """Algo no cierra. No se escribe nada."""


# --------------------------------------------------------------------- salida

class Informe:
    def __init__(self):
        self.avisos: list[str] = []
        self.alertas: list[str] = []
        self.lineas: list[str] = []

    def ok(self, txt): self.lineas.append(f"   {txt}")
    def aviso(self, txt): self.avisos.append(txt); self.lineas.append(f"   ~ {txt}")
    def alerta(self, txt): self.alertas.append(txt); self.lineas.append(f"   ! {txt}")
    def titulo(self, txt): self.lineas.append(f"\n{txt}\n" + "-" * len(txt))

    def imprimir(self):
        print("\n".join(self.lineas))
        if self.alertas:
            print("\n" + "=" * 74)
            print(f"  {len(self.alertas)} ALERTA(S) QUE REQUIEREN TU OJO")
            print("=" * 74)
            for a in self.alertas:
                print(f"  ! {a}")
        if self.avisos:
            print(f"\n  ({len(self.avisos)} aviso(s) menor(es))")


SIN_LIMITE_DECIMAL = 6  # el campo no tiene decimal.precision atado


def precision_precio(o: Odoo) -> int:
    """Decimales con los que Odoo guarda un precio unitario.

    NO se deduce del registro `decimal.precision` llamado "Product Price": ese
    gobierna otros campos. Preguntarselo a `fields_get`, que devuelve los digitos
    ya resueltos del campo de verdad.

    En esta instancia (Odoo 19) `account.move.line.price_unit` viene con
    `digits=False`, o sea SIN restriccion de decimales — Odoo lo guarda como
    float completo. Eso es lo que permite derivar el precio del subtotal y que la
    factura entre al centavo. Asumir 2 decimales por el nombre del registro fue
    un error de metodo, corregido el 27/08/2026.
    """
    try:
        info = o.call("account.move.line", "fields_get",
                      [["price_unit"]], {"attributes": ["digits"]})
        digits = (info.get("price_unit") or {}).get("digits")
    except OdooError:
        return 2
    if isinstance(digits, (list, tuple)) and len(digits) == 2:
        return int(digits[1])
    if digits in (False, None):
        return SIN_LIMITE_DECIMAL
    try:
        return int(digits)
    except (TypeError, ValueError):
        return 2


def pesos(x) -> str:
    """Formato argentino: $ 1.234.567,89"""
    return "$ " + f"{x:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")


# ------------------------------------------------------- validación aritmética

def validar_aritmetica(doc: dict, inf: Informe) -> None:
    """Reconcilia el JSON contra sí mismo antes de tocar Odoo.

    Es el control que atrapa los errores de lectura del PDF: una cantidad mal
    leída casi siempre rompe la aritmética de la línea.
    """
    inf.titulo("1 · Reconciliación aritmética del comprobante")

    suma = 0.0
    for i, l in enumerate(doc["lineas"], 1):
        cant = l.get("cantidad_unidades", l.get("cantidad"))
        if cant is None:
            raise Frenar(f"Línea {i} ({l.get('descripcion')}): no trae cantidad.")
        bonif = 1 - (l.get("bonificacion_pct", 0) / 100)
        esperado = cant * l["precio_unitario"] * bonif
        declarado = l["subtotal"]
        tol = max(TOL_LINEA_ABS, abs(declarado) * TOL_LINEA_PCT)
        if abs(esperado - declarado) > tol:
            desc_bonif = f" − {l['bonificacion_pct']}%" if l.get("bonificacion_pct") else ""
            raise Frenar(
                f"Línea {i} ({l.get('descripcion')}): {cant} × {l['precio_unitario']}"
                f"{desc_bonif} = {pesos(esperado)}, pero la factura dice {pesos(declarado)}. "
                f"Diferencia {pesos(abs(esperado - declarado))}. "
                f"Revisá la lectura del PDF con `leer_pdf.py --columnas`."
            )
        suma += declarado
        if l.get("_revisar"):
            inf.alerta(f"Línea {i} ({l.get('descripcion')}): {l['_revisar']}")

    t = doc["totales"]
    if abs(suma - t["neto"]) > TOL_TOTAL_ABS:
        raise Frenar(
            f"La suma de las líneas da {pesos(suma)} y el neto declarado es "
            f"{pesos(t['neto'])}. Falta o sobra una línea."
        )
    inf.ok(f"Líneas: {len(doc['lineas'])}  ·  suma {pesos(suma)} = neto declarado ✓")

    percep = sum(p["importe"] for p in t.get("percepciones", []))
    total_calc = t["neto"] + t.get("iva", 0) + percep
    if abs(total_calc - t["total"]) > TOL_TOTAL_ABS:
        raise Frenar(
            f"neto {pesos(t['neto'])} + IVA {pesos(t.get('iva', 0))} + percepciones "
            f"{pesos(percep)} = {pesos(total_calc)}, pero el total impreso es "
            f"{pesos(t['total'])}."
        )
    inf.ok(f"Neto + IVA + percepciones = {pesos(t['total'])} = total impreso ✓")


# ------------------------------------------------------------------ proveedor

def resolver_proveedor(o: Odoo, doc: dict, inf: Informe) -> dict:
    inf.titulo("2 · Proveedor")
    p = doc["proveedor"]
    encontrado = o.buscar_proveedor(p["cuit"], p.get("nombre", ""))
    if not encontrado:
        raise Frenar(
            f"No hay ningún contacto en Odoo con el CUIT {p['cuit']} "
            f"({p.get('nombre')}). Cargalo primero como proveedor — no lo creo yo "
            f"para no ensuciar la base con un duplicado del que ya exista."
        )
    inf.ok(f"[{encontrado['id']}] {encontrado['name']}  ·  CUIT {encontrado.get('vat')}")
    return encontrado


# ---------------------------------------------------------------- duplicados

def _alnum(txt: str) -> str:
    return "".join(c for c in (txt or "") if c.isalnum()).upper()


def _digitos(txt: str) -> str:
    return "".join(c for c in (txt or "") if c.isdigit())


def verificar_duplicado(o: Odoo, partner_id: int, doc: dict, inf: Informe,
                        permitir: bool = False, simulacion: bool = False) -> None:
    """Control de duplicados en dos niveles.

    Comparar el número limpiando sólo guiones y espacios NO alcanza. Kairon
    guarda los comprobantes con un prefijo de tipo — `FA0003-00000018` — así que
    un `0003-00000018` recién leído del PDF nunca coincidía, y el control dejaba
    pasar duplicados de facturas ya contabilizadas. Se detectó el 27/08/2026,
    justo antes de duplicar la FA 0003-00000018 de Nutregal.

    Ahora hay dos niveles:

      · **Duro** — coincide la secuencia alfanumérica completa, o coinciden los
        dígitos y el importe. Frena.
      · **Blando** — coinciden los dígitos pero no el importe. Suele ser una
        nota de crédito con la misma numeración (`NCA0003-...` vs `FA0003-...`).
        Avisa y sigue.
    """
    inf.titulo("3 · Control de duplicados")
    comp = doc["comprobante"]
    numero = comp["numero"]
    total = doc["totales"]["total"]

    campo_num = o.primer_campo("account.move", "l10n_latam_document_number")
    campos = ["name", "ref", "state", "invoice_date", "move_type", "amount_total"]
    if campo_num:
        campos.append(campo_num)

    existentes = o.buscar_leer(
        "account.move",
        [["move_type", "=", "in_refund" if doc['comprobante'].get('tipo') == 'NOTA_CREDITO' else "in_invoice"], ["partner_id", "=", partner_id]],
        campos,
    )

    clave = _alnum(numero)
    digitos = _digitos(numero)
    coincidencias = []

    for m in existentes:
        for c in ("ref", "name", campo_num):
            if not c:
                continue
            valor = m.get(c) or ""
            if not valor:
                continue
            if _alnum(valor) == clave:
                coincidencias.append((m, "el número coincide exactamente", True))
                break
            if digitos and _digitos(valor) == digitos:
                mismo = abs((m.get("amount_total") or 0) - total) < 0.05
                coincidencias.append((
                    m,
                    "mismos dígitos y mismo importe" if mismo
                    else "mismos dígitos, distinto importe",
                    mismo,
                ))
                break

    duros = [c for c in coincidencias if c[2]]
    blandos = [c for c in coincidencias if not c[2]]

    for m, _motivo, _ in blandos:
        inf.alerta(
            f"Puede ser un duplicado: {m['name']} ({m.get('state')}, "
            f"{m.get('invoice_date')}, {pesos(m.get('amount_total') or 0)}) tiene los mismos "
            f"dígitos que {numero} pero otro importe. Suele ser una nota de crédito con la "
            f"misma numeración. Revisalo antes de contabilizar."
        )

    if duros:
        m, motivo, _ = duros[0]
        detalle = (f"{m['name']} · {m.get('state')} · {m.get('invoice_date')} · "
                   f"{pesos(m.get('amount_total') or 0)} · ref={m.get('ref')}")
        if permitir:
            inf.alerta(f"DUPLICADO IGNORADO a pedido (--ignorar-duplicado): {detalle} ({motivo}).")
        elif simulacion:
            # En simulacion no frenamos: el informe sirve para ver TODO lo que
            # pasaria. Con --escribir esto corta la carga.
            inf.alerta(
                f"YA ESTA CARGADO: {detalle} ({motivo}). "
                f"Sigo el informe porque esto es una simulacion; con --escribir FRENA."
            )
        else:
            raise Frenar(
                f"Ese comprobante ya está cargado: {detalle}\n"
                f"   Coincidencia: {motivo}.\n"
                f"   Si de verdad querés cargarlo igual, agregá --ignorar-duplicado."
            )
    elif not blandos:
        inf.ok(f"El comprobante {numero} no está cargado para este proveedor ✓")


# ------------------------------------------------------- productos y unidades

def resolver_lineas(o: Odoo, doc: dict, proveedor: dict, cfg: dict, inf: Informe) -> list[dict]:
    """Mapea cada línea de la factura a un producto de Odoo y su UdM.

    El mapeo va por `product.supplierinfo.product_code`, que es el código con el
    que el proveedor imprime el producto. No es el `default_code` de Odoo.
    """
    inf.titulo("4 · Productos y unidades de medida")
    campo_factor = cfg.get("campo_factor_uom") or o.primer_campo("uom.uom", "factor", "relative_factor")
    dp = precision_precio(o)
    modo_carga = (cfg.get("unidad_de_carga") or "bulto").lower()
    inf.ok(f"Decimales de account.move.line.price_unit: "
           + (f"sin límite (uso {dp})" if dp == SIN_LIMITE_DECIMAL else str(dp)))
    inf.ok(f"Unidad de carga: {modo_carga}"
           + ("  (cajas/bultos, como el historial de Kairon)" if modo_carga == "bulto"
              else "  (la UdM del producto en Odoo)"))
    resueltas = []
    sin_mapear = []

    for i, l in enumerate(doc["lineas"], 1):
        si = o.producto_por_codigo_proveedor(proveedor["id"], l["codigo_proveedor"])
        if not si:
            sin_mapear.append(f"{l['codigo_proveedor']}  {l.get('descripcion')}")
            continue

        pid = si.get("product_id") or si.get("product_tmpl_id")
        pid = pid[0] if isinstance(pid, list) else pid
        modelo = "product.product" if si.get("product_id") else "product.template"
        if modelo == 'product.template':
            variants = o.buscar_leer('product.product', [['product_tmpl_id', '=', pid]], ['id'], limite=2)
            if len(variants) != 1:
                sin_mapear.append(f"{l['codigo_proveedor']} (producto con variantes ambiguas)")
                continue
            pid = variants[0]['id']
            modelo = 'product.product'
        prod = o.uno(modelo, [["id", "=", pid]],
                     ["name", "default_code", "uom_id", "standard_price",
                      "supplier_taxes_id", "x_studio_unidades_por_caja"])
        if not prod:
            sin_mapear.append(f"{l['codigo_proveedor']}  {l.get('descripcion')} (supplierinfo huérfano)")
            continue

        uom = prod.get("uom_id")
        uom_id = uom[0] if isinstance(uom, list) else uom
        uom_nombre = uom[1] if isinstance(uom, list) else "?"
        udm = o.uno("uom.uom", [["id", "=", uom_id]], ["name", campo_factor]) or {}
        factor = udm.get(campo_factor) or 1

        # Camino A: la factura declara unidades sueltas (Nutregal: BULT × UxB).
        #
        # En que unidad se carga es una decision de negocio, no tecnica, y vive
        # en config.json -> unidad_de_carga:
        #
        #   "bulto"        - como viene cargando Kairon a mano desde siempre:
        #                    12 lineas a $ 26.360,05, no 192 a $ 1.647,50. Es lo
        #                    que mantiene la continuidad del historial de precios.
        #   "udm_producto" - usa el factor de la UdM del producto. Hoy todos los
        #                    Murken estan en «Units» factor 1, o sea 192 sueltas.
        divisor = 1
        if l.get("cantidad_unidades"):
            unidades = l["cantidad_unidades"]
            if modo_carga == "bulto":
                divisor = (l.get("unidades_por_bulto")
                           or prod.get("x_studio_unidades_por_caja") or 1)
                if not l.get("unidades_por_bulto"):
                    inf.aviso(
                        f"Línea {i} ({prod['name']}): la factura no dice cuántas "
                        f"unidades trae el bulto. Uso x_studio_unidades_por_caja = {divisor}."
                    )
                origen = f"{unidades} u ÷ {divisor:g} u/bulto"
            else:
                divisor = factor
                origen = f"{unidades} u ÷ factor {factor}"
            cantidad = unidades / divisor
            precio = l["precio_unitario"] * divisor
        # Camino B: la factura da cantidad sin decir de qué. Se asume la UdM del
        # producto en Odoo y se marca para revisión.
        else:
            cantidad = l["cantidad"]
            precio = l["precio_unitario"]
            origen = f"{cantidad} en la UdM del proveedor"
            inf.aviso(
                f"Línea {i} ({prod['name']}): la factura no dice en qué unidad "
                f"viene la cantidad. Asumo «{uom_nombre}», la UdM del producto en "
                f"Odoo. Verificá contra el remito."
            )

        # El precio impreso viene redondeado (Nutregal usa 3 decimales) y el
        # subtotal no. Derivarlo del subtotal reproduce la factura al centavo;
        # usar el impreso arrastra centavos en cada línea y termina cargando la
        # factura por un importe distinto al del papel.
        bonif = 1 - (l.get("bonificacion_pct", 0) / 100)
        if cantidad and bonif:
            ideal = round(l["subtotal"] / bonif / cantidad, dp)
            resto_ideal = abs(ideal * cantidad * bonif - l["subtotal"])
            resto_impreso = abs(precio * cantidad * bonif - l["subtotal"])
            if resto_ideal < resto_impreso:
                if abs(ideal - precio) > abs(precio) * 0.005:
                    inf.alerta(
                        f"Línea {i} ({prod['name']}): el precio impreso "
                        f"{pesos(precio)} y el que sale del subtotal {pesos(ideal)} "
                        f"difieren más de 0,5%. No lo cambio — revisá la lectura del PDF."
                    )
                else:
                    if resto_impreso > 0.01:
                        inf.aviso(
                            f"Línea {i}: uso {pesos(ideal)} en vez del precio impreso "
                            f"{pesos(precio)}; así el subtotal da exacto "
                            f"(el impreso dejaba {pesos(resto_impreso)} de diferencia)."
                        )
                    precio = ideal

        # Chequeo cruzado: cantidad × precio tiene que reproducir el subtotal.
        recalculo = cantidad * precio * bonif
        if abs(recalculo - l["subtotal"]) > max(TOL_LINEA_ABS, abs(l["subtotal"]) * TOL_LINEA_PCT):
            raise Frenar(
                f"Línea {i} ({prod['name']}): después de convertir a «{uom_nombre}» "
                f"da {pesos(recalculo)} y la factura dice {pesos(l['subtotal'])}. "
                f"La unidad de medida del producto en Odoo no coincide con la de la "
                f"factura. No escribo nada hasta que se resuelva."
            )

        ucaja = prod.get("x_studio_unidades_por_caja")
        if ucaja and factor and abs(ucaja - factor) > 0.001 and factor != 1:
            inf.aviso(
                f"{prod['name']}: x_studio_unidades_por_caja = {ucaja} pero el "
                f"factor de «{uom_nombre}» es {factor}. Uno de los dos está mal."
            )
        if not ucaja:
            inf.alerta(
                f"{prod['name']} no tiene x_studio_unidades_por_caja cargado. "
                f"Todos los cálculos en cajas de este producto van a salir mal."
            )

        resueltas.append({
            "indice": i,
            "factura": l,
            "producto": prod,
            "product_id": pid,
            "uom_id": uom_id,
            "uom_nombre": uom_nombre,
            "factor": factor,
            "factor_carga": divisor,
            "cantidad": round(cantidad, 4),
            "precio_unitario": round(precio, 4),
            "supplierinfo": si,
        })
        inf.ok(f"[{i:>2}] {l['codigo_proveedor']:<12} → [{pid}] {prod['name'][:44]:<44} "
               f"{cantidad:>8.3f} {uom_nombre:<10} × {pesos(precio)}   ({origen})")

    if sin_mapear:
        raise Frenar(
            "Estos códigos del proveedor no están mapeados a ningún producto:\n     "
            + "\n     ".join(sin_mapear)
            + "\n\n   Cargalos en la pestaña «Compra» del producto (product.supplierinfo), "
              "con el proveedor y el código. Una vez cargado, no hay que volver a hacerlo."
        )
    return resueltas


# -------------------------------------------------------- control de precios

def control_precios(o: Odoo, resueltas: list[dict], proveedor: dict, cfg: dict, inf: Informe) -> None:
    """Compara cada precio facturado contra el histórico. Tolerancia configurable.

    Contra la OC no se compara cuando la OC la genera este mismo script a partir
    de la factura: ahí el precio coincide por construcción y el control no
    detectaría nada. La comparación que sirve es contra lo que ya pasó.
    """
    inf.titulo("5 · Control de precios")
    tol = cfg.get("control_precios", {}).get("tolerancia_pct", 0.0) / 100
    # La tolerancia sigue siendo 0%: cualquier cambio de precio se avisa. Pero una
    # diferencia por debajo de un centavo de redondeo no es un cambio de precio.
    piso = cfg.get("control_precios", {}).get("piso_absoluto", 0.05)

    for r in resueltas:
        prod = r["producto"]
        precio_nuevo = r["precio_unitario"]
        referencias = []

        # a) último precio efectivamente facturado por este proveedor
        historico = o.buscar_leer(
            "account.move.line",
            [["product_id", "=", r["product_id"]],
             ["parent_state", "=", "posted"],
             ["move_id.move_type", "=", "in_invoice"]],
            ["price_unit", "quantity", "date", "move_id", "product_uom_id"],
            limite=1, orden="date desc, id desc",
        )
        if historico:
            h = historico[0]
            referencias.append(("último precio de compra", h["price_unit"], str(h.get("date"))))

        # b) y c) estan cargados POR UNIDAD (la UdM del producto). Si la factura
        # se carga por bulto hay que llevarlos a la misma base, o la comparacion
        # no significa nada: $ 26.360 la caja contra $ 1.647 la unidad da +1500%
        # y no es un aumento de precio, es una diferencia de unidad.
        escala = r.get("factor_carga") or 1
        sufijo = f" ×{escala:g}" if escala != 1 else ""

        # b) precio de lista cargado en la ficha del proveedor
        if r["supplierinfo"].get("price"):
            referencias.append((f"lista del proveedor{sufijo}",
                                r["supplierinfo"]["price"] * escala, "supplierinfo"))

        # c) costo estándar del producto
        if prod.get("standard_price"):
            referencias.append((f"costo estándar{sufijo}",
                                prod["standard_price"] * escala, "product"))

        if not referencias:
            inf.aviso(f"{prod['name']}: primera compra, no hay contra qué comparar.")
            continue

        for etiqueta, anterior, cuando in referencias:
            if not anterior:
                continue
            delta = (precio_nuevo - anterior) / anterior
            if abs(delta) > tol and abs(precio_nuevo - anterior) > piso:
                signo = "▲" if delta > 0 else "▼"
                inf.alerta(
                    f"{prod['name']}: {signo} {delta * 100:+.2f}% vs {etiqueta} "
                    f"({cuando}). Antes {pesos(anterior)} → ahora {pesos(precio_nuevo)}."
                )
            else:
                inf.ok(f"{prod['name'][:40]:<40} = {etiqueta} ✓")


# ------------------------------------------------------------------- diario

def elegir_diario(o: Odoo, doc: dict, cfg: dict, inf: Informe) -> dict:
    """Factura con validez fiscal → diario Compras. Sin validez fiscal → Compra interno."""
    inf.titulo("6 · Diario y tipo de comprobante")
    comp = doc["comprobante"]

    fiscal = comp.get("clase") == "fiscal"
    razones = []
    if comp.get("cae"):
        razones.append("tiene CAE")
    if comp.get("letra") in ("A", "B", "C", "M"):
        razones.append(f"letra {comp['letra']}")
    if doc["totales"].get("iva"):
        razones.append("IVA discriminado")
    if comp.get("letra") == "X" or comp.get("tipo", "").upper() in ("GUIA", "REMITO"):
        razones.append(f"{comp.get('tipo')} letra {comp.get('letra')} — sin validez fiscal")

    clave = "fiscal" if fiscal else "interno"
    entrada = (cfg.get("diarios") or {}).get(clave)
    if not entrada or not entrada.get("id"):
        raise Frenar(
            f"No hay diario configurado para comprobantes de clase «{clave}». "
            f"Completá config.json → diarios.{clave} con el ID del diario "
            f"({'Compras' if fiscal else 'Compra interno'}). Corré `descubrir.py` "
            f"para ver los IDs disponibles."
        )
    diario = o.uno("account.journal", [["id", "=", entrada["id"]]],
                   ["name", "code", "type", "l10n_latam_use_documents"])
    if not diario:
        raise Frenar(f"El diario con ID {entrada['id']} no existe en Odoo.")

    inf.ok(f"Clase: {clave}  ({', '.join(razones) or 'sin señales fiscales'})")
    inf.ok(f"Diario: [{diario['id']}] {diario['name']}")
    inf.ok(f"Número de comprobante: {comp['numero']}")
    return diario


# ---------------------------------------------------------------- impuestos

def resolver_impuestos(o: Odoo, doc: dict, cfg: dict, inf: Informe) -> dict:
    """Devuelve los IDs de impuestos a aplicar por alícuota, más las percepciones."""
    if doc['comprobante'].get('clase') == 'interno':
        return {'por_alicuota': {}, 'percepciones': []}
    inf.titulo("7 · Impuestos")
    imp_cfg = cfg.get("impuestos") or {}
    mapa: dict[float, int] = {}

    alicuotas = {l.get("alicuota_iva", 0) for l in doc["lineas"]}
    for a in alicuotas:
        clave = {21: "iva_21", 10.5: "iva_10_5", 0: "iva_0"}.get(a)
        entrada = imp_cfg.get(clave) if clave else None
        if not entrada or not entrada.get("id"):
            if a == 0:
                mapa[a] = None
                inf.ok("Líneas sin IVA: se cargan sin impuesto")
                continue
            raise Frenar(
                f"La factura tiene líneas con IVA {a}% y no hay impuesto configurado "
                f"para eso en config.json → impuestos.{clave}. No lo adivino."
            )
        mapa[a] = entrada["id"]
        inf.ok(f"IVA {a}% → [{entrada['id']}] {entrada.get('nombre')}")

    percepciones = []
    for p in doc["totales"].get("percepciones", []):
        entrada = imp_cfg.get("percepcion_iva")
        if not entrada or not entrada.get("id"):
            raise Frenar(
                f"La factura trae «{p['detalle']}» por {pesos(p['importe'])} y no hay "
                f"impuesto de percepción configurado. Si lo cargo sin eso, la factura "
                f"queda por {pesos(doc['totales']['total'] - p['importe'])} en vez de "
                f"{pesos(doc['totales']['total'])}. Completá config.json → "
                f"impuestos.percepcion_iva."
            )
        percepciones.append({"tax_id": entrada["id"], "detalle": p["detalle"], "importe": p["importe"]})
        inf.ok(f"{p['detalle']} → [{entrada['id']}] {entrada.get('nombre')}  {pesos(p['importe'])}")

    return {"por_alicuota": mapa, "percepciones": percepciones}


# ---------------------------------------------------------------- escritura

def crear_orden_compra(o: Odoo, doc: dict, proveedor: dict, resueltas: list[dict],
                       impuestos: dict, cfg: dict, confirmar: bool, inf: Informe) -> int:
    inf.titulo("8 · Orden de compra")
    campo_uom = o.primer_campo("purchase.order.line", "product_uom_id", "product_uom")
    # En Odoo 19 el campo es `tax_ids`; `taxes_id` no existe. Escribirlo a ciegas
    # rompía la creación de la orden de compra.
    campo_tax = o.primer_campo("purchase.order.line", "tax_ids", "taxes_id")
    comp = doc["comprobante"]

    lineas = []
    for r in resueltas:
        vals = {
            "product_id": r["product_id"],
            "name": r["producto"]["name"],
            "product_qty": r["cantidad"],
            "price_unit": r["precio_unitario"],
            "date_planned": comp["fecha"],
        }
        if campo_uom:
            vals[campo_uom] = r["uom_id"]
        tax = impuestos["por_alicuota"].get(r["factura"].get("alicuota_iva", 0))
        extras = [p["tax_id"] for p in impuestos["percepciones"]]
        ids = [t for t in ([tax] + extras) if t]
        if campo_tax:
            vals[campo_tax] = [(6, 0, ids)]
        elif ids:
            inf.alerta("purchase.order.line no tiene campo de impuestos: la OC queda sin IVA.")
        if r["factura"].get("bonificacion_pct") and o.tiene("purchase.order.line", "discount"):
            vals["discount"] = r["factura"]["bonificacion_pct"]
        lineas.append((0, 0, vals))

    oc_vals = {
        "partner_id": proveedor["id"],
        "date_order": f"{comp['fecha']} 00:00:00",
        "partner_ref": f"{comp.get('prefijo_ref', '')}{comp['numero']}",
        "order_line": lineas,
    }
    if o.tiene("purchase.order", "notes"):
        oc_vals["notes"] = (
            f"Generada automáticamente desde {comp.get('tipo')} {comp.get('letra','')} "
            f"{comp['numero']} del {comp['fecha']}."
        )

    oc_id = o.crear("purchase.order", oc_vals)
    oc = o.uno("purchase.order", [["id", "=", oc_id]], ["name", "state", "amount_total"])
    inf.ok(f"Creada {oc['name']} (id {oc_id}) en estado «{oc['state']}» · {pesos(oc['amount_total'])}")

    if confirmar:
        o.call("purchase.order", "button_confirm", [[oc_id]])
        oc = o.uno("purchase.order", [["id", "=", oc_id]], ["name", "state", "picking_ids"])
        pickings = oc.get("picking_ids") or []
        inf.ok(f"Confirmada. Estado «{oc['state']}». Ingresos generados: {pickings} (sin validar)")
    else:
        inf.ok("Queda como solicitud de presupuesto. El ingreso de mercadería aparece "
               "recién cuando la confirmes.")
    return oc_id


def crear_factura(o: Odoo, doc: dict, proveedor: dict, resueltas: list[dict],
                  impuestos: dict, diario: dict, cfg: dict, oc_id: int | None,
                  vinculada: bool, inf: Informe, *, original_id=None) -> int:
    inf.titulo("9 · Factura de proveedor")
    comp = doc["comprobante"]
    campo_uom = o.primer_campo("account.move.line", "product_uom_id")

    lineas_oc = {}
    if vinculada and oc_id:
        for l in o.buscar_leer("purchase.order.line", [["order_id", "=", oc_id]],
                               ["product_id", "product_qty", "price_unit"]):
            pid = l["product_id"][0] if isinstance(l["product_id"], list) else l["product_id"]
            lineas_oc.setdefault(pid, []).append(l["id"])

    lineas = []
    for r in resueltas:
        vals = {
            "product_id": r["product_id"],
            "name": r["producto"]["name"],
            "quantity": r["cantidad"],
            "price_unit": r["precio_unitario"],
        }
        if campo_uom:
            vals[campo_uom] = r["uom_id"]
        tax = impuestos["por_alicuota"].get(r["factura"].get("alicuota_iva", 0))
        extras = [p["tax_id"] for p in impuestos["percepciones"]]
        ids = [t for t in ([tax] + extras) if t]
        vals["tax_ids"] = [(6, 0, ids)]
        if r["factura"].get("bonificacion_pct") and o.tiene("account.move.line", "discount"):
            vals["discount"] = r["factura"]["bonificacion_pct"]
        if vinculada and o.tiene("account.move.line", "purchase_line_id"):
            pendientes = lineas_oc.get(r["product_id"])
            if pendientes:
                vals["purchase_line_id"] = pendientes.pop(0)
        lineas.append((0, 0, vals))

    mv = {
        "move_type": "in_refund" if comp.get('tipo') == 'NOTA_CREDITO' else "in_invoice",
        "partner_id": proveedor["id"],
        "journal_id": diario["id"],
        "invoice_date": comp["fecha"],
        "date": comp["fecha"],
        "ref": f"{comp.get('prefijo_ref', '')}{comp['numero']}",
        "invoice_line_ids": lineas,
    }
    if original_id:
        mv['reversed_entry_id'] = original_id
    if comp.get("fecha_vencimiento"):
        mv["invoice_date_due"] = comp["fecha_vencimiento"]
    if o.tiene("account.move", "invoice_origin") and oc_id:
        oc = o.uno("purchase.order", [["id", "=", oc_id]], ["name"])
        mv["invoice_origin"] = oc["name"] if oc else ""

    # Numeración fiscal argentina
    if diario.get("l10n_latam_use_documents") and o.tiene("account.move", "l10n_latam_document_number"):
        mv["l10n_latam_document_number"] = comp["numero"]
        tipo = (cfg.get("tipos_documento") or {}).get(
            f"{'nota_credito' if comp.get('tipo') == 'NOTA_CREDITO' else 'factura'}_{(comp.get('letra') or '').lower()}"
        )
        if tipo and tipo.get("id"):
            mv["l10n_latam_document_type_id"] = tipo["id"]
            inf.ok(f"Tipo de documento: [{tipo['id']}] {tipo.get('nombre')}")
        else:
            raise Frenar(
                f"El diario «{diario['name']}» usa documentos de la localización y no "
                f"hay tipo de documento configurado para «FACTURA {comp.get('letra')}». "
                f"Completá config.json → tipos_documento."
            )

    if comp.get("cae"):
        for campo, valor in (("l10n_ar_afip_auth_code", comp["cae"]),
                             ("l10n_ar_afip_auth_code_due", comp.get("cae_vencimiento"))):
            if valor and o.tiene("account.move", campo):
                mv[campo] = valor
        inf.ok(f"CAE {comp['cae']} · vence {comp.get('cae_vencimiento')}")

    notas = [f"Cargada automáticamente desde el PDF del proveedor."]
    if not vinculada and oc_id:
        oc = o.uno("purchase.order", [["id", "=", oc_id]], ["name"])
        notas.append(f"Orden de compra asociada: {oc['name']} (confirmala para generar el ingreso).")
    if comp.get("condicion_venta"):
        notas.append(f"Condición de venta: {comp['condicion_venta']}.")
    if o.tiene("account.move", "narration"):
        if doc.get('observaciones_carga'):
            import html
            notas.append(html.escape(doc['observaciones_carga']))
        mv["narration"] = " ".join(notas)

    move_id = o.crear("account.move", mv)
    m = o.uno("account.move", [["id", "=", move_id]],
              ["name", "state", "amount_untaxed", "amount_tax", "amount_total"])
    inf.ok(f"Creada {m['name']} (id {move_id}) en estado «{m['state']}»")
    inf.ok(f"Neto {pesos(m['amount_untaxed'])} · impuestos {pesos(m['amount_tax'])} "
           f"· total {pesos(m['amount_total'])}")

    esperado = doc["totales"]["total"]
    dif = abs(m["amount_total"] - esperado)
    # Tres niveles. El del medio existe porque una diferencia de centavos que no
    # llega al umbral duro se estaba tragando en silencio, y despues aparece en
    # la conciliacion de la cuenta corriente.
    if dif > TOL_TOTAL_ABS:
        inf.alerta(
            f"El total que calculó Odoo ({pesos(m['amount_total'])}) no coincide con el "
            f"del PDF ({pesos(esperado)}). Diferencia {pesos(dif)}. "
            f"Revisá los impuestos antes de contabilizar."
        )
    elif dif > 0.01:
        inf.aviso(
            f"El total de Odoo ({pesos(m['amount_total'])}) difiere del PDF "
            f"({pesos(esperado)}) en {pesos(dif)}. Es redondeo, pero se acumula "
            f"factura a factura en la cuenta corriente."
        )
    else:
        inf.ok(f"El total de Odoo coincide con el del PDF ✓")
    return move_id


# ------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("factura", type=Path, help="JSON estructurado de la factura")
    ap.add_argument("--escribir", action="store_true", help="escribir en Odoo (por defecto simula)")
    ap.add_argument("--confirmar-oc", action="store_true",
                    help="confirmar la OC para que aparezca el ingreso y la factura quede vinculada")
    ap.add_argument("--ignorar-duplicado", action="store_true",
                    help="cargar aunque el comprobante ya exista (para pruebas; deja alerta)")
    ap.add_argument("--config", type=Path, default=RAIZ / "config.json")
    args = ap.parse_args()

    doc = json.loads(args.factura.read_text(encoding="utf-8"))
    cfg = json.loads(args.config.read_text(encoding="utf-8")) if args.config.exists() else {}
    if not cfg:
        print(f"No encontré {args.config}. Corré primero:  python3 descubrir.py --config",
              file=sys.stderr)
        return 1

    inf = Informe()
    comp = doc["comprobante"]
    print("=" * 74)
    print(f"  {comp.get('tipo')} {comp.get('letra','')} {comp['numero']}  ·  "
          f"{doc['proveedor']['nombre']}  ·  {pesos(doc['totales']['total'])}")
    print(f"  {'ESCRITURA REAL' if args.escribir else 'SIMULACIÓN — no se escribe nada'}")
    print("=" * 74)

    try:
        validar_aritmetica(doc, inf)
        o = Odoo.desde_config()
        proveedor = resolver_proveedor(o, doc, inf)
        verificar_duplicado(o, proveedor["id"], doc, inf,
                            args.ignorar_duplicado, simulacion=not args.escribir)
        resueltas = resolver_lineas(o, doc, proveedor, cfg, inf)
        control_precios(o, resueltas, proveedor, cfg, inf)
        diario = elegir_diario(o, doc, cfg, inf)
        impuestos = resolver_impuestos(o, doc, cfg, inf)

        if not args.escribir:
            inf.titulo("8 · Escritura")
            inf.ok("Simulación: hasta acá todo cierra. Volvé a correrlo con --escribir.")
            inf.imprimir()
            return 0

        oc_id = crear_orden_compra(o, doc, proveedor, resueltas, impuestos, cfg,
                                   args.confirmar_oc, inf)
        crear_factura(o, doc, proveedor, resueltas, impuestos, diario, cfg,
                      oc_id, args.confirmar_oc, inf)

        inf.titulo("Listo")
        inf.ok("Quedó todo en borrador. Nada se contabilizó ni se validó.")
        inf.imprimir()
        return 0

    except Frenar as e:
        inf.imprimir()
        print(f"\n{'=' * 74}\n  FRENADO — no se escribió nada\n{'=' * 74}\n  {e}\n", file=sys.stderr)
        return 2
    except OdooError as e:
        inf.imprimir()
        print(f"\n✗ Odoo: {e}\n", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
