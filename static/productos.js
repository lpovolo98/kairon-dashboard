/* Agente de productos — ABM masivo sobre Odoo.
   Regla de toda la pantalla: previsualizar contra Odoo real, y no escribir
   una sola fila hasta que el usuario confirme. Las filas sin cambios no se
   escriben nunca. */

const API = window.location.origin;

const ESTADO = {
  hoja: 'productos',
  catalogo: null,        // categorias, unidades, impuestos, listas, proveedores
  filas: { productos: [], proveedores: [], precios: [] },
  crudas: { productos: [], proveedores: [], precios: [] },
  fotos: [],             // {archivo, dataUrl, pesoOriginal, pesoFinal, sku, origen}
  sinFoto: [],
  pendientes: [],
  fotoSel: null,
  filtro: null,          // estado seleccionado en los chips
};

const LADO_MAXIMO = 1600;   // mismos parametros que achicar_imagenes.py
const CALIDAD_JPEG = 0.85;

// ── Utilidades ────────────────────────────────────────────
const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const fmtMB = b => b >= 1048576 ? (b / 1048576).toFixed(1) + ' MB' : Math.round(b / 1024) + ' KB';

// Compara sin acentos ni signos: "Pan Molde Clásico" ≡ "pan molde clasico".
const normalizar = s => String(s ?? '')
  .normalize('NFKD').replace(/[̀-ͯ]/g, '')
  .toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();

async function pedir(ruta, opciones) {
  const r = await fetch(`${API}${ruta}`, opciones);
  if (!r.ok) throw new Error(`${ruta} respondió ${r.status}`);
  return r.json();
}

// ── Arranque ──────────────────────────────────────────────
async function iniciar() {
  try {
    const st = await pedir('/api/productos/status');
    const box = $('#conexion');
    if (st.ready && st.persistente === false) {
      // Sin volumen persistente el historial se pierde al reiniciar, y con él
      // la posibilidad de revertir lo que ya se aplicó.
      box.className = 'status';
      box.innerHTML = 'Conectado a Odoo, pero el historial de corridas <b>no es persistente</b> '
        + `(<code>${esc(st.datos)}</code>): si el servidor se reinicia, lo aplicado no se va a poder revertir.`;
    } else if (st.ready) {
      box.className = 'status online';
      box.textContent = 'Conectado a Odoo · las altas y modificaciones se previsualizan antes de escribir';
    } else {
      box.textContent = 'Falta configurar: ' + (st.missing || []).join(', ');
    }
    ESTADO.catalogo = await pedir('/api/productos/catalogo');
    prepararMasiva();
  } catch (e) {
    $('#conexion').textContent = 'No se pudo conectar con el servicio. Revisá la configuración.';
  }
  try {
    ESTADO.pendientes = await pedir('/api/productos/pendientes');
    pintarPendientes();
  } catch (e) { /* la cola es opcional */ }
}

// ── Pestañas ──────────────────────────────────────────────
$$('.tab').forEach(t => t.addEventListener('click', () => {
  $$('.tab').forEach(x => x.classList.toggle('activa', x === t));
  $$('.panel').forEach(p => p.classList.remove('activo'));
  $('#panel-' + t.dataset.tab).classList.add('activo');
  ESTADO.hoja = t.dataset.tab;
  actualizarBarra();
}));

// ══════════════════════════════════════════════════════════
//  PRODUCTOS · lectura y previsualización
// ══════════════════════════════════════════════════════════

/* Pegado desde Excel: llega TSV. Se respeta el encabezado tal cual porque
   es el que ya usa la plantilla vigente. */
function leerTSV(texto) {
  const lineas = texto.trim().split(/\r?\n/).filter(l => l.trim());
  if (lineas.length < 2) return [];
  const cols = lineas[0].split('\t').map(c => c.trim());
  return lineas.slice(1).map(l => {
    const celdas = l.split('\t');
    return Object.fromEntries(cols.map((c, i) => [c, (celdas[i] ?? '').trim()]));
  });
}

async function previsualizar(hojas) {
  // Se guardan las filas crudas: al aplicar se vuelven a mandar y el servidor
  // recalcula todo. El navegador nunca manda una escritura.
  ESTADO.crudas = hojas;
  ESTADO.filas = await pedir('/api/productos/previsualizar', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(hojas),
  });
  pintarTodo();
}

/* Las ediciones en la tabla vuelven a la fila cruda y se re-previsualiza:
   el estado, el diff y los errores los recalcula siempre el servidor. */
async function reprevisualizar() {
  $$('#tabla-productos tbody tr').forEach(tr => {
    const fila = ESTADO.crudas.productos[+tr.dataset.i];
    if (!fila) return;
    tr.querySelectorAll('[data-col]').forEach(td => {
      const control = td.querySelector('.celda-edit');
      if (control) fila[td.dataset.col] = control.value;
    });
  });
  await previsualizar(ESTADO.crudas);
}

function pintarTodo() {
  pintarPreview('productos');
  pintarSecundaria('proveedores');
  pintarSecundaria('precios');
  $$('.tab').forEach(t => {
    const n = (ESTADO.filas[t.dataset.tab] || []).length;
    const chip = t.querySelector('.n');
    if (chip && ESTADO.filas[t.dataset.tab]) chip.textContent = n;
  });
  actualizarBarra();
}

const ETIQUETA = { alta: 'Alta', modificacion: 'Modifica', igual: 'Sin cambios', error: 'Error' };
const CLASE    = { alta: 'e-alta', modificacion: 'e-mod', igual: 'e-igual', error: 'e-error' };

function contar(filas) {
  const c = { alta: 0, modificacion: 0, igual: 0, error: 0 };
  filas.forEach(f => { c[f.estado] = (c[f.estado] || 0) + 1; });
  return c;
}

function pintarPreview(hoja) {
  const filas = ESTADO.filas[hoja] || [];
  $('#productos-entrada').hidden = filas.length > 0;
  $('#productos-preview').hidden = filas.length === 0;
  if (!filas.length) return;

  const c = contar(filas);
  const campoDe = f => Object.keys(f.campos || {});
  const nCampos = filas.filter(f => f.estado === 'modificacion')
                       .reduce((s, f) => s + campoDe(f).filter(k => f.campos[k].viejo !== undefined).length, 0);

  $('#resumen-productos').innerHTML = [
    ['alta', 'p-alta', c.alta, 'altas'],
    ['modificacion', 'p-mod', c.modificacion, `modificaciones · ${nCampos} campos`],
    ['igual', 'p-igual', c.igual, 'sin cambios · no se tocan'],
    ['error', 'p-error', c.error, 'con error'],
  ].map(([k, p, n, txt]) => `
    <div class="chip ${ESTADO.filtro === k ? 'sel' : ''}" data-filtro="${k}">
      <span class="punto ${p}"></span><b>${n}</b><span>${txt}</span>
    </div>`).join('');

  $$('#resumen-productos .chip').forEach(ch => ch.addEventListener('click', () => {
    ESTADO.filtro = ESTADO.filtro === ch.dataset.filtro ? null : ch.dataset.filtro;
    pintarPreview(hoja);
  }));

  // Las columnas salen de los datos, no de una lista fija: si mañana la
  // plantilla trae un campo mas, aparece solo.
  const columnas = [...new Set(filas.flatMap(campoDe))];
  const tabla = $('#tabla-productos');
  tabla.querySelector('thead').innerHTML = `<tr>
    <th>Estado</th><th>SKU</th>${columnas.map(c => `<th data-col="${esc(c)}">${esc(c)}</th>`).join('')}</tr>`;

  tabla.querySelector('tbody').innerHTML = filas.map((f, i) => `
    <tr data-i="${i}" class="${ESTADO.filtro && f.estado !== ESTADO.filtro ? 'oculta' : ''}">
      <td><span class="estado ${CLASE[f.estado]}">${ETIQUETA[f.estado]}</span>
          ${(f.errores || []).map(e => `<span class="err">${esc(e)}</span>`).join('')}</td>
      <td class="sku">${esc(f.sku)}</td>
      ${columnas.map(col => `<td data-col="${esc(col)}">${celda(f, col)}</td>`).join('')}
    </tr>`).join('');

  $$('#tabla-productos .celda-edit').forEach(el =>
    el.addEventListener('change', reprevisualizar));
  filtrarTexto();
}

/* Una celda muestra el valor nuevo; si ademas hay un valor previo distinto,
   muestra el viejo tachado al lado. Las que tienen opciones cerradas en
   Odoo (categoria, unidad, listas) son un desplegable, para que el error
   "no existe esa categoria" no pueda ocurrir. */
function celda(fila, col) {
  const d = (fila.campos || {})[col];
  if (!d) return '<span style="color:var(--dim)">—</span>';
  const opciones = (ESTADO.catalogo || {})[d.opciones];
  const viejo = d.viejo !== undefined && d.viejo !== d.nuevo
    ? `<span class="viejo">${esc(d.viejo || '—')}</span><span class="flecha">→</span>` : '';
  const clase = d.viejo !== undefined && d.viejo !== d.nuevo ? 'nuevo' : '';

  if (opciones) {
    return viejo + `<select class="celda-edit ${clase}">
      ${opciones.map(o => `<option ${o.name === d.nuevo ? 'selected' : ''}>${esc(o.name)}</option>`).join('')}
      ${opciones.some(o => o.name === d.nuevo) ? '' : `<option selected>${esc(d.nuevo)}</option>`}
    </select>`;
  }
  return viejo + `<input class="celda-edit ${clase}" value="${esc(d.nuevo)}">`;
}

function pintarSecundaria(hoja) {
  const filas = ESTADO.filas[hoja] || [];
  const cont = $('#' + hoja + '-cuerpo');
  if (!filas.length) return;
  const c = contar(filas);
  const columnas = [...new Set(filas.flatMap(f => Object.keys(f.campos || {})))];
  cont.innerHTML = `
    <div class="resumen">
      <div class="chip"><span class="punto p-alta"></span><b>${c.alta}</b><span>nuevos</span></div>
      <div class="chip"><span class="punto p-mod"></span><b>${c.modificacion}</b><span>actualizados</span></div>
      <div class="chip"><span class="punto p-igual"></span><b>${c.igual}</b><span>sin cambios</span></div>
      <div class="chip"><span class="punto p-error"></span><b>${c.error}</b><span>con error</span></div>
    </div>
    <div class="tabla-wrap"><table><thead><tr><th>Estado</th><th>SKU</th>
      ${columnas.map(x => `<th data-col="${esc(x)}">${esc(x)}</th>`).join('')}</tr></thead>
      <tbody>${filas.map(f => `<tr>
        <td><span class="estado ${CLASE[f.estado]}">${ETIQUETA[f.estado]}</span>
            ${(f.errores || []).map(e => `<span class="err">${esc(e)}</span>`).join('')}</td>
        <td class="sku">${esc(f.sku)}</td>
        ${columnas.map(col => `<td data-col="${esc(col)}">${celda(f, col)}</td>`).join('')}
      </tr>`).join('')}</tbody></table></div>`;
}

function filtrarTexto() {
  const q = normalizar($('#buscar-productos').value);
  $$('#tabla-productos tbody tr').forEach(tr => {
    const f = ESTADO.filas.productos[+tr.dataset.i];
    const coincide = !q || normalizar(f.sku + ' ' + (f.campos?.Nombre?.nuevo || '')).includes(q);
    const pasaEstado = !ESTADO.filtro || f.estado === ESTADO.filtro;
    tr.classList.toggle('oculta', !(coincide && pasaEstado));
  });
}

// ── Barra de acción ───────────────────────────────────────
function actualizarBarra() {
  const barra = $('#accionbar');
  if (ESTADO.hoja === 'imagenes') {
    const asignadas = ESTADO.fotos.filter(f => f.sku).length;
    barra.classList.toggle('visible', asignadas > 0);
    $('#accionbar-texto').innerHTML = `<b>${asignadas}</b> foto${asignadas === 1 ? '' : 's'} lista${asignadas === 1 ? '' : 's'} para subir · las que ya tengan imagen se pisan, y se puede revertir.`;
    $('#btn-aplicar').textContent = `Subir ${asignadas} imagen${asignadas === 1 ? '' : 'es'}`;
    $('#btn-aplicar').disabled = asignadas === 0;
    return;
  }
  const filas = ESTADO.filas[ESTADO.hoja] || [];
  if (!filas.length) { barra.classList.remove('visible'); return; }
  const c = contar(filas);
  const aEscribir = c.alta + c.modificacion;
  barra.classList.add('visible');
  $('#accionbar-texto').innerHTML = c.error
    ? `<span style="color:var(--red)">Hay ${c.error} fila${c.error === 1 ? '' : 's'} con error.</span> Corregilas en la tabla: no se escribe nada hasta que no quede ninguna.`
    : `Se van a crear <b>${c.alta}</b> y modificar <b>${c.modificacion}</b>. Las ${c.igual} sin cambios no se tocan.`;
  $('#btn-aplicar').textContent = `Crear ${c.alta} · Modificar ${c.modificacion}`;
  $('#btn-aplicar').disabled = c.error > 0 || aEscribir === 0;
}

// ══════════════════════════════════════════════════════════
//  IMÁGENES
// ══════════════════════════════════════════════════════════

/* Achica en el navegador antes de subir: una foto de 27 MB sale como ~300 KB.
   Es lo que hacia achicar_imagenes.py, pero del lado del cliente, asi el
   original nunca viaja ni ocupa memoria en el servidor. */
function achicar(archivo) {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(archivo);
    const img = new Image();
    img.onload = () => {
      const escala = Math.min(1, LADO_MAXIMO / Math.max(img.width, img.height));
      const lienzo = document.createElement('canvas');
      lienzo.width = Math.round(img.width * escala);
      lienzo.height = Math.round(img.height * escala);
      const ctx = lienzo.getContext('2d');
      ctx.drawImage(img, 0, 0, lienzo.width, lienzo.height);
      URL.revokeObjectURL(url);
      const dataUrl = lienzo.toDataURL('image/jpeg', CALIDAD_JPEG);
      // El dataURL es base64: el peso real en bytes es 3/4 de su largo.
      resolve({ dataUrl, pesoFinal: Math.round(dataUrl.length * 0.75),
                ancho: lienzo.width, alto: lienzo.height });
    };
    img.onerror = () => { URL.revokeObjectURL(url); reject(new Error('No se pudo leer ' + archivo.name)); };
    img.src = url;
  });
}

/* Propone el producto de una foto por el nombre del archivo. Primero busca
   un SKU adentro del nombre; si no hay, compara palabra por palabra contra
   los nombres de producto. Reemplaza al diccionario MAPEO escrito a mano. */
function proponerSku(nombreArchivo, productos) {
  const base = nombreArchivo.replace(/\.[^.]+$/, '');
  const exacto = productos.find(p => base.includes(p.sku));
  if (exacto) return { sku: exacto.sku, origen: 'sku' };

  const palabras = normalizar(base).split(' ').filter(w => w.length > 2);
  if (!palabras.length) return { sku: null, origen: 'ninguno' };

  // Cuantos productos usan cada palabra. Una palabra que aparece en uno solo
  // ("bizcochuelo") identifica tanto como un codigo; una que aparece en
  // veinte ("pan") no dice nada. Sin esto, "bizcochuelo C.png" se pierde.
  const frecuencia = new Map();
  const tokens = productos.map(p => {
    const t = new Set(normalizar(p.nombre).split(' ').filter(w => w.length > 2));
    t.forEach(w => frecuencia.set(w, (frecuencia.get(w) || 0) + 1));
    return t;
  });

  let mejor = null, mejorPuntaje = 0, mejorUnica = false;
  productos.forEach((p, i) => {
    const dePro = tokens[i];
    if (!dePro.size) return;
    const comunes = palabras.filter(w => dePro.has(w));
    if (!comunes.length) return;
    const puntaje = comunes.length / Math.max(palabras.length, dePro.size);
    const unica = comunes.some(w => frecuencia.get(w) === 1);
    // Una coincidencia por palabra unica gana siempre sobre una por solapamiento.
    if ((unica && !mejorUnica) || (unica === mejorUnica && puntaje > mejorPuntaje)) {
      mejorPuntaje = puntaje; mejor = p; mejorUnica = unica;
    }
  });
  if (mejor && (mejorUnica || mejorPuntaje >= 0.4)) return { sku: mejor.sku, origen: 'nombre' };
  return { sku: null, origen: 'ninguno' };
}

async function cargarFotos(archivos) {
  const imagenes = [...archivos].filter(a => /^image\//.test(a.type));
  if (!imagenes.length) return;
  try { ESTADO.sinFoto = await pedir('/api/productos/sin-imagen'); } catch (e) { ESTADO.sinFoto = []; }

  $('#nombre-imagenes').textContent = `Procesando ${imagenes.length} fotos…`;
  ESTADO.fotos = [];
  for (const archivo of imagenes) {
    try {
      const r = await achicar(archivo);
      const p = proponerSku(archivo.name, ESTADO.sinFoto);
      ESTADO.fotos.push({ archivo: archivo.name, dataUrl: r.dataUrl,
        pesoOriginal: archivo.size, pesoFinal: r.pesoFinal,
        medidas: `${r.ancho}×${r.alto}`, sku: p.sku, origen: p.origen });
    } catch (e) { /* un archivo ilegible no frena a los demas */ }
  }
  $('#imagenes-entrada').hidden = true;
  $('#imagenes-board').hidden = false;
  pintarFotos();
}

function pintarFotos() {
  const fotos = ESTADO.fotos;
  const auto = fotos.filter(f => f.origen === 'sku').length;
  const sug  = fotos.filter(f => f.origen === 'nombre').length;
  const sin  = fotos.filter(f => !f.sku).length;
  const ahorro = fotos.reduce((s, f) => s + f.pesoOriginal - f.pesoFinal, 0);

  $('#resumen-imagenes').innerHTML = `
    <div class="chip"><span class="punto p-alta"></span><b>${auto}</b><span>por código en el nombre</span></div>
    <div class="chip"><span class="punto p-mod"></span><b>${sug}</b><span>sugeridas · revisá</span></div>
    <div class="chip"><span class="punto p-error"></span><b>${sin}</b><span>sin asignar</span></div>
    <div class="chip"><span class="punto p-igual"></span><b>${fmtMB(ahorro)}</b><span>menos para subir</span></div>`;

  const opciones = ESTADO.sinFoto;
  $('#grid-imagenes').innerHTML = fotos.map((f, i) => {
    const clase = !f.sku ? 'huerfana' : f.origen === 'sku' ? 'asignada' : 'sugerida';
    const prod = opciones.find(p => p.sku === f.sku);
    return `<div class="foto ${clase} ${ESTADO.fotoSel === i ? 'sel' : ''}" data-i="${i}">
      <img class="miniatura" src="${f.dataUrl}" alt="${esc(f.archivo)}" loading="lazy">
      <div class="foto-info">
        <div class="foto-archivo" title="${esc(f.archivo)}">${esc(f.archivo)}</div>
        <div class="foto-sku ${f.sku ? '' : 'sin'}">${f.sku ? esc(f.sku) + (prod ? ' · ' + esc(prod.nombre) : '') : 'Sin asignar'}</div>
        <div class="foto-peso">${fmtMB(f.pesoOriginal)} → <b>${fmtMB(f.pesoFinal)}</b> · ${f.medidas}</div>
        <select data-i="${i}">
          <option value="">— sin asignar —</option>
          ${opciones.map(p => `<option value="${esc(p.sku)}" ${p.sku === f.sku ? 'selected' : ''}>${esc(p.sku)} — ${esc(p.nombre)}</option>`).join('')}
        </select>
      </div></div>`;
  }).join('');

  const libres = opciones.filter(p => !fotos.some(f => f.sku === p.sku));
  $('#sub-sin-foto').textContent = `${libres.length} de ${opciones.length} siguen sin imagen.`;
  $('#lista-sin-foto').innerHTML = libres.length
    ? libres.map(p => `<div class="sin-foto-item" data-sku="${esc(p.sku)}">
        <div class="ph">▣</div><div><b>${esc(p.nombre)}</b><small>${esc(p.sku)}</small></div></div>`).join('')
    : '<p class="vacio">Todos los productos quedaron con foto.</p>';

  $$('#grid-imagenes .miniatura').forEach(el => el.addEventListener('click', () => {
    const i = +el.closest('.foto').dataset.i;
    ESTADO.fotoSel = ESTADO.fotoSel === i ? null : i;
    pintarFotos();
  }));
  $$('#grid-imagenes select').forEach(sel => sel.addEventListener('change', () => {
    ESTADO.fotos[+sel.dataset.i].sku = sel.value || null;
    ESTADO.fotos[+sel.dataset.i].origen = sel.value ? 'manual' : 'ninguno';
    pintarFotos();
  }));
  // Tablero: foto seleccionada + click en un producto = asignada.
  $$('.sin-foto-item').forEach(el => el.addEventListener('click', () => {
    if (ESTADO.fotoSel === null) return;
    ESTADO.fotos[ESTADO.fotoSel].sku = el.dataset.sku;
    ESTADO.fotos[ESTADO.fotoSel].origen = 'manual';
    ESTADO.fotoSel = null;
    pintarFotos();
  }));
  actualizarBarra();
}

// ── Pendientes del agente administrativo ──────────────────
function pintarPendientes() {
  const p = ESTADO.pendientes;
  $('#n-pendientes').textContent = p.length;
  $('#pendientes-cuerpo').innerHTML = p.length ? `
    <div class="tabla-wrap"><table><thead><tr>
      <th>Proveedor</th><th>Código</th><th>Descripción</th><th class="num">Precio</th><th>Origen</th><th></th>
    </tr></thead><tbody>${p.map(x => `<tr>
      <td>${esc(x.proveedor)}</td><td class="sku">${esc(x.codigo_proveedor)}</td>
      <td>${esc(x.descripcion)}</td><td class="num">$${esc(x.precio)}</td>
      <td class="sku">Factura ${esc(x.factura)}</td>
      <td><button class="secundario chico">Dar de alta</button></td>
    </tr>`).join('')}</tbody></table></div>
    <div class="aviso">Al crearlos, el agente administrativo puede retomar la factura donde la dejó.</div>`
    : '<p class="vacio">No hay productos pendientes. Cuando el agente administrativo encuentre un código que no existe, va a aparecer acá.</p>';
}

// ══════════════════════════════════════════════════════════
//  MODIFICACIÓN MASIVA SIN ARCHIVO
// ══════════════════════════════════════════════════════════

// Qué operaciones tienen sentido según el tipo del campo: multiplicar un
// nombre no significa nada.
const OPERACIONES = {
  fijar:       { texto: 'Fijar en',        tipos: ['texto', 'numero', 'm2o', 'm2m', 'booleano'] },
  multiplicar: { texto: 'Multiplicar por', tipos: ['numero'] },
  sumar:       { texto: 'Sumar',           tipos: ['numero'] },
  reemplazar:  { texto: 'Buscar y reemplazar', tipos: ['texto'] },
};

function opciones(lista, incluirVacio) {
  return (incluirVacio ? '<option value="">Todas</option>' : '')
    + (lista || []).map(x => `<option>${esc(x.name || x.col)}</option>`).join('');
}

function prepararMasiva() {
  const c = ESTADO.catalogo || {};
  $('#m-categoria').innerHTML = '<option value="">Todas</option>' + opciones(c.categorias);
  $('#m-proveedor').innerHTML = '<option value="">Todos</option>' + opciones(c.proveedores);
  $('#m-columna').innerHTML = (c.columnas || []).map(x => `<option>${esc(x.col)}</option>`).join('');
  $('#m-columna').addEventListener('change', pintarOperaciones);
  $('#m-operacion').addEventListener('change', pintarValor);
  pintarOperaciones();
}

function tipoDeColumna() {
  const col = $('#m-columna').value;
  return ((ESTADO.catalogo.columnas || []).find(x => x.col === col) || {}).tipo || 'texto';
}

function pintarOperaciones() {
  const tipo = tipoDeColumna();
  $('#m-operacion').innerHTML = Object.entries(OPERACIONES)
    .filter(([, o]) => o.tipos.includes(tipo))
    .map(([k, o]) => `<option value="${k}">${o.texto}</option>`).join('');
  pintarValor();
}

function pintarValor() {
  const op = $('#m-operacion').value;
  $('#m-valor-caja').innerHTML = op === 'reemplazar'
    ? 'Buscar / reemplazar por<div style="display:flex;gap:8px">'
      + '<input id="m-valor" placeholder="texto viejo"><input id="m-valor2" placeholder="texto nuevo"></div>'
    : 'Valor<input id="m-valor" placeholder="' + (op === 'multiplicar' ? '1.15 para un 15%' : '') + '">';
}

$('#btn-masiva').addEventListener('click', async () => {
  const boton = $('#btn-masiva');
  const filtros = {
    categoria: $('#m-categoria').value, proveedor: $('#m-proveedor').value,
    texto: $('#m-texto').value.trim(),
  };
  if (!filtros.categoria && !filtros.proveedor && !filtros.texto)
    return alert('Elegí al menos un filtro: si no, se traerían todos los productos.');

  const op = $('#m-operacion').value;
  const valor = op === 'reemplazar'
    ? [$('#m-valor').value, $('#m-valor2').value]
    : $('#m-valor').value.trim();
  if (op === 'reemplazar' ? !valor[0] : !valor) return alert('Falta el valor.');

  boton.disabled = true; $('#m-info').textContent = 'Buscando en Odoo…';
  try {
    const r = await pedir('/api/productos/operacion', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...filtros, columna: $('#m-columna').value, operacion: op, valor }),
    });
    ESTADO.crudas = { productos: r.filas, proveedores: [], precios: [] };
    ESTADO.filas = r.plan;
    $('#m-info').textContent = '';
    pintarTodo();
  } catch (e) {
    $('#m-info').textContent = e.message;
  } finally {
    boton.disabled = false;
  }
});

// ── Eventos ───────────────────────────────────────────────
function conectarDrop(zona, input, alSoltar) {
  ['dragenter', 'dragover'].forEach(e => zona.addEventListener(e, ev => {
    ev.preventDefault(); zona.classList.add('dragging');
  }));
  ['dragleave', 'drop'].forEach(e => zona.addEventListener(e, ev => {
    ev.preventDefault(); zona.classList.remove('dragging');
  }));
  zona.addEventListener('drop', ev => alSoltar(ev.dataTransfer.files));
  input.addEventListener('change', () => alSoltar(input.files));
}

conectarDrop($('#drop-imagenes'), $('#file-imagenes'), cargarFotos);
conectarDrop($('#drop-productos'), $('#file-productos'), files => {
  if (files[0]) $('#nombre-productos').textContent = files[0].name;
});

$('#btn-leer-productos').addEventListener('click', async () => {
  const texto = $('#pegar-productos').value.trim();
  if (texto) return previsualizar({ productos: leerTSV(texto), proveedores: [], precios: [] });
  const archivo = $('#file-productos').files[0];
  if (!archivo) return alert('Subí un archivo o pegá las celdas desde Excel.');
  try {
    // El .xlsx lo lee el servidor: trae las tres hojas de una. Va como cuerpo
    // crudo, igual que los PDF del agente administrativo.
    previsualizar(await pedir(`/api/productos/leer?nombre=${encodeURIComponent(archivo.name)}`,
                              { method: 'POST', body: archivo }));
  } catch (e) {
    alert('No se pudo leer el archivo: ' + e.message);
  }
});
$('#btn-otro-archivo').addEventListener('click', () => {
  ESTADO.filas = { productos: [], proveedores: [], precios: [] };
  ESTADO.filtro = null;
  $('#productos-entrada').hidden = false;
  $('#productos-preview').hidden = true;
  actualizarBarra();
});
$('#btn-otras-fotos').addEventListener('click', () => {
  ESTADO.fotos = []; ESTADO.fotoSel = null;
  $('#imagenes-entrada').hidden = false;
  $('#imagenes-board').hidden = true;
  $('#nombre-imagenes').textContent = 'Arrastrá las fotos acá';
  actualizarBarra();
});
$('#btn-plantilla').addEventListener('click', () => {
  window.location = `${API}/api/productos/plantilla.xlsx`;
});
$('#buscar-productos').addEventListener('input', filtrarTexto);
$('#btn-aplicar').addEventListener('click', async () => {
  const boton = $('#btn-aplicar');
  const textoOriginal = boton.textContent;
  boton.disabled = true; boton.textContent = 'Aplicando…';
  try {
    const r = ESTADO.hoja === 'imagenes'
      ? await pedir('/api/productos/imagenes', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ asignaciones: ESTADO.fotos.filter(f => f.sku)
            .map(f => ({ sku: f.sku, imagen: f.dataUrl })) }) })
      : await pedir('/api/productos/aplicar', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(ESTADO.crudas) });
    mostrarResultado(r);
  } catch (e) {
    boton.disabled = false; boton.textContent = textoOriginal;
    alert('No se pudo aplicar: ' + e.message);
  }
});

function mostrarResultado(r) {
  const n = (r.creados || r.subidas || []).length;
  const m = (r.modificados || []).length;
  $('#accionbar').classList.remove('visible');
  const caja = document.createElement('div');
  caja.className = 'card';
  caja.style.marginTop = '20px';
  caja.innerHTML = `
    <div class="section-head"><div>
      <h2>Listo</h2>
      <p>Se ${r.subidas ? 'subieron' : 'crearon'} <b>${n}</b>${m ? ` y se modificaron <b>${m}</b>` : ''} registro${n + m === 1 ? '' : 's'}.
      ${(r.errores || []).length ? `<span style="color:var(--red)">${r.errores.length} con problemas.</span>` : ''}</p>
    </div><button class="fantasma" id="btn-revertir">Revertir esta corrida</button></div>
    ${(r.errores || []).map(e => `<p class="err">${esc(e)}</p>`).join('')}`;
  $('#panel-' + (ESTADO.hoja === 'imagenes' ? 'imagenes' : 'productos')).prepend(caja);
  $('#btn-revertir').addEventListener('click', async () => {
    if (!confirm('Se van a devolver los campos modificados a su valor anterior y a archivar lo creado. ¿Seguir?')) return;
    await pedir(`/api/productos/corridas/${r.id}/revertir`, { method: 'POST' });
    location.reload();
  });
}

$('#btn-cancelar').addEventListener('click', () => {
  (ESTADO.hoja === 'imagenes' ? $('#btn-otras-fotos') : $('#btn-otro-archivo')).click();
});

iniciar();
