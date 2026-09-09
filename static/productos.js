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
  busy: false, revision: null, requestId: null, archivo: null, secuencia: 0,
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
  let r;
  try { r = await fetch(`${API}${ruta}`, {...opciones, redirect:'manual', headers:{Accept:'application/json',...opciones?.headers}}); }
  catch { throw new Error('No se pudo conectar. Revisá el historial antes de repetir una carga.'); }
  if(r.type==='opaqueredirect'||r.status===401||r.status===403) throw new Error('Volvé a ingresar al portal. Tu borrador queda guardado en este equipo.');
  if(!(r.headers.get('content-type')||'').includes('application/json')) throw new Error('El servicio devolvió una respuesta inesperada. Revisá el historial antes de repetir.');
  if (!r.ok) {
    // El servidor explica qué falta; un "respondió 503" pelado no sirve.
    let detalle = `${ruta} respondió ${r.status}`;
    try { const j = await r.json(); if (j.detail) detalle = j.detail; } catch (e) {}
    throw new Error(detalle);
  }
  try { return await r.json(); } catch { throw new Error('La respuesta llegó incompleta. Actualizá el historial.'); }
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
      box.textContent = 'Odoo producción · revisá y confirmá los cambios antes de aplicar';
    } else {
      box.textContent = 'Falta configurar: ' + (st.missing || []).join(', ');
    }
    ESTADO.catalogo = await pedir('/api/productos/catalogo');
    prepararMasiva();
    // Si una lista de Odoo no se pudo leer, se avisa cuál: el resto de la
    // pantalla sigue funcionando, pero ese desplegable va a estar vacío.
    const avisos = ESTADO.catalogo.avisos || [];
    if (avisos.length) {
      box.className = 'status';
      box.innerHTML = `Conectado a Odoo, pero ${avisos.length} lista(s) no se pudieron leer:`
        + avisos.map(a => `<br><code>${esc(a)}</code>`).join('');
    }
  } catch (e) {
    $('#conexion').textContent = e.message || 'No se pudo conectar con el servicio.';
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
  ESTADO.crudas = hojas; ESTADO.revision=null; ESTADO.requestId=null;
  const sequence=++ESTADO.secuencia; ESTADO.busy=true; actualizarBarra();
  try { localStorage.setItem('kairon-productos-borrador',JSON.stringify(hojas)); } catch {}
  try {
  const result = await pedir('/api/productos/previsualizar', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(hojas),
  });
  if(sequence!==ESTADO.secuencia)return;
  ESTADO.filas=result; ESTADO.revision=result._revision;
  pintarTodo();
  } catch(e) { aviso(e.message); }
  finally {if(sequence===ESTADO.secuencia){ESTADO.busy=false;actualizarBarra();}}
}

/* Las ediciones en la tabla vuelven a la fila cruda y se re-previsualiza:
   el estado, el diff y los errores los recalcula siempre el servidor. */
async function reprevisualizar() {
  $$('tr[data-hoja][data-i]').forEach(tr => {
    const fila = ESTADO.crudas[tr.dataset.hoja][+tr.dataset.i];
    if (!fila) return;
    tr.querySelectorAll('[data-col]').forEach(td => {
      const control = td.querySelector('.celda-edit');
      if (control) fila[td.dataset.col] = control.multiple ? ([...control.selectedOptions].map(o=>o.value).join('; ') || '[VACIAR]') : control.value;
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
  const columnas = [...new Set(filas.flatMap(campoDe))].filter(col=>!$('#solo-cambios').checked || filas.some(f=>['alta','error'].includes(f.estado)||f.campos[col]?.viejo!==undefined));
  const tabla = $('#tabla-productos');
  tabla.querySelector('thead').innerHTML = `<tr>
    <th>Estado</th><th>SKU</th>${columnas.map(c => `<th data-col="${esc(c)}">${esc(c)}</th>`).join('')}<th>Selección</th></tr>`;

  tabla.querySelector('tbody').innerHTML = filas.map((f, i) => `
    <tr data-hoja="productos" data-i="${i}" class="${ESTADO.filtro && f.estado !== ESTADO.filtro ? 'oculta' : ''}">
      <td><span class="estado ${CLASE[f.estado]}">${ETIQUETA[f.estado]}</span>
          ${(f.errores || []).map(e => `<span class="err">${esc(e)}</span>`).join('')}</td>
      <td class="sku">${esc(f.sku)}</td>
      ${columnas.map(col => `<td data-col="${esc(col)}">${celda(f, col)}</td>`).join('')}
      <td><button class="fantasma chico" data-excluir="${esc(f.sku)}">Quitar del lote</button></td>
    </tr>`).join('');

  $$('#tabla-productos .celda-edit').forEach(el =>
    el.addEventListener('change', reprevisualizar));
  $$('[data-excluir]').forEach(button=>button.onclick=()=>{
    const sku=button.dataset.excluir.trim().toLowerCase();
    const hojas=Object.fromEntries(['productos','proveedores','precios'].map(h=>[h,(ESTADO.crudas[h]||[]).filter(row=>String(row[h==='productos'?'Referencia interna (SKU)':'SKU (igual al de Productos)']||'').trim().toLowerCase()!==sku)]));
    if(Object.values(hojas).some(rows=>rows.length))previsualizar(hojas);else $('#btn-otro-archivo').click();
  });
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
    const multi=String(d.opciones).startsWith('impuestos');
    const ids=Array.isArray(d.seleccion)?d.seleccion:[d.seleccion];
    return viejo + `<select ${multi?'multiple':''} class="celda-edit ${clase}" aria-label="${esc(col)}">
      ${opciones.map(o => `<option value="${esc(o.name)} (id ${o.id})" ${ids.includes(o.id) ? 'selected' : ''}>${esc(o.name)} · ${o.id}</option>`).join('')}
      ${!ids.some(Boolean)&&d.nuevo&&d.nuevo!=='[VACIAR]' ? `<option selected>${esc(d.nuevo)}</option>`:''}
    </select>`;
  }
  return viejo + `<input class="celda-edit ${clase}" value="${esc(d.nuevo)}">`;
}

function pintarSecundaria(hoja) {
  const filas = ESTADO.filas[hoja] || [];
  const cont = $('#' + hoja + '-cuerpo');
  if (!filas.length) {cont.innerHTML='<p class="vacio">No hay filas en esta hoja.</p>';return;}
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
      <tbody>${filas.map((f,i) => `<tr data-hoja="${hoja}" data-i="${i}">
        <td><span class="estado ${CLASE[f.estado]}">${ETIQUETA[f.estado]}</span>
            ${(f.errores || []).map(e => `<span class="err">${esc(e)}</span>`).join('')}</td>
        <td class="sku">${esc(f.sku)}</td>
        ${columnas.map(col => `<td data-col="${esc(col)}">${celda(f, col)}</td>`).join('')}
      </tr>`).join('')}</tbody></table></div>`;
  cont.querySelectorAll('.celda-edit').forEach(el=>el.addEventListener('change',reprevisualizar));
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
    $('#btn-aplicar').disabled = asignadas === 0 || ESTADO.busy;
    return;
  }
  const filas = ['productos','proveedores','precios'].flatMap(h=>ESTADO.filas[h]||[]);
  if (!filas.length) { barra.classList.remove('visible'); return; }
  const c = contar(filas);
  const aEscribir = c.alta + c.modificacion;
  barra.classList.add('visible');
  $('#accionbar-texto').innerHTML = c.error
    ? `<span style="color:var(--red)">Hay ${c.error} fila${c.error === 1 ? '' : 's'} con error.</span> Corregilas en la tabla: no se escribe nada hasta que no quede ninguna.`
    : ['productos','proveedores','precios'].map(h=>{const n=contar(ESTADO.filas[h]||[]);return `<b>${n.alta+n.modificacion}</b> cambios en ${h}`;}).join(' · ') + '. Los campos vacíos opcionales se conservan.';
  $('#btn-aplicar').textContent = `Crear ${c.alta} · Modificar ${c.modificacion}`;
  $('#btn-aplicar').disabled = c.error > 0 || aEscribir === 0 || ESTADO.busy || !ESTADO.revision;
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
  const exactos=productos.filter(p=>normalizar(base).split(' ').includes(normalizar(p.sku)));
  const exacto = exactos.length===1?exactos[0]:null;
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
  ESTADO.requestId=null;
  const imagenes = [...archivos].filter(a => /^image\//.test(a.type));
  if (!imagenes.length) return;
  try { ESTADO.sinFoto = await pedir('/api/productos/sin-imagen?todos=true'); } catch (e) {aviso(e.message);return;}

  $('#nombre-imagenes').textContent = `Procesando ${imagenes.length} fotos…`;
  ESTADO.fotos = [];
  for (const archivo of imagenes) {
    try {
      const r = await achicar(archivo);
      const p = proponerSku(archivo.name, ESTADO.sinFoto);
      ESTADO.fotos.push({ archivo: archivo.name, dataUrl: r.dataUrl,
        pesoOriginal: archivo.size, pesoFinal: r.pesoFinal,
        medidas: `${r.ancho}×${r.alto}`, sku: p.origen==='sku'?p.sku:null, sugerencia:p.sku, origen: p.origen });
    } catch (e) { aviso('Archivo rechazado: '+archivo.name); }
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
    const prod = opciones.find(p => p.sku === (f.sku||f.sugerencia));
    return `<div class="foto ${clase} ${ESTADO.fotoSel === i ? 'sel' : ''}" data-i="${i}">
      <img class="miniatura" src="${f.dataUrl}" alt="${esc(f.archivo)}" loading="lazy">
      ${prod?.tiene_imagen&&f.sku?`<details class="foto-info"><summary>Comparar con foto actual</summary><img src="/api/productos/foto?sku=${encodeURIComponent(f.sku)}" alt="Foto actual en Odoo" loading="lazy" width="128" height="128"></details>`:''}
      <div class="foto-info">
        <div class="foto-archivo" title="${esc(f.archivo)}">${esc(f.archivo)}</div>
        <div class="foto-sku ${f.sku ? '' : 'sin'}">${f.sku ? esc(f.sku) + (prod ? ' · ' + esc(prod.nombre) : '') : f.sugerencia?'Propuesta: '+esc(f.sugerencia)+' · '+esc(prod?.nombre||'')+' · Seleccionala para confirmar':'Sin asignar'}</div>
        <div class="foto-peso">${fmtMB(f.pesoOriginal)} → <b>${fmtMB(f.pesoFinal)}</b> · ${f.medidas}</div>
        <select data-i="${i}">
          <option value="">— sin asignar —</option>
          ${opciones.map(p => `<option value="${esc(p.sku)}" ${p.sku === f.sku ? 'selected' : ''}>${esc(p.sku)} — ${esc(p.nombre)}</option>`).join('')}
        </select>
      </div></div>`;
  }).join('');

  const libres = opciones.filter(p => !fotos.some(f => f.sku === p.sku));
  $('#sub-sin-foto').textContent = `${libres.length} productos disponibles para asignar. Se indica si ya tienen foto.`;
  $('#lista-sin-foto').innerHTML = libres.length
    ? libres.map(p => `<div class="sin-foto-item" data-sku="${esc(p.sku)}">
        <div class="ph">▣</div><div><b>${esc(p.nombre)}</b><small>${esc(p.sku)} · ${p.tiene_imagen?'Reemplazar foto':'Sin foto'}</small></div></div>`).join('')
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
      <td><button class="secundario chico" data-pendiente="${esc(x.id)}">Resolver producto</button></td>
    </tr>`).join('')}</tbody></table></div>
    <div class="aviso">Al crearlos, el agente administrativo puede retomar la factura donde la dejó.</div>`
    : '<p class="vacio">No hay productos pendientes. Cuando el agente administrativo encuentre un código que no existe, va a aparecer acá.</p>';
  $$('[data-pendiente]').forEach(button=>button.onclick=()=>abrirPendiente(p.find(x=>x.id===button.dataset.pendiente)));
}

function abrirPendiente(item){
 const dialog=document.createElement('dialog');dialog.className='card';
 dialog.innerHTML=`<form><h2>Resolver producto de factura</h2><p>${esc(item.proveedor)} · ${esc(item.descripcion)}</p>
 <label>SKU existente o nuevo<input name="sku" required></label>
 <label>Nombre para un producto nuevo<input name="nombre" value="${esc(item.descripcion)}"></label>
 <label>Unidades por caja si es nuevo<input name="unidades" type="number" min="1" step="1"></label>
 <label>Precio de compra por unidad de Odoo<input name="precio" type="number" min="0" step="0.0001" required></label>
 <p>El comprobante indica ${esc(item.precio)}. Verificá si corresponde a una unidad o una caja.</p>
 <button>Preparar para revisión</button><button type="button" data-cerrar>Cancelar</button></form>`;
 document.body.append(dialog);dialog.showModal();dialog.querySelector('[data-cerrar]').onclick=()=>dialog.remove();
 dialog.querySelector('form').onsubmit=async e=>{e.preventDefault();const body=Object.fromEntries(new FormData(e.target));
 try{const r=await pedir(`/api/productos/pendientes/${item.id}/preparar`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
 ESTADO.crudas=r.filas;ESTADO.filas=r.plan;ESTADO.revision=r.plan._revision;ESTADO.requestId=null;
 $('[data-tab="productos"]').click();pintarTodo();dialog.remove();
 }catch(error){aviso(error.message);}};
}

// ══════════════════════════════════════════════════════════
//  MODIFICACIÓN MASIVA SIN ARCHIVO
// ══════════════════════════════════════════════════════════

// Qué operaciones tienen sentido según el tipo del campo: multiplicar un
// nombre no significa nada.
const OPERACIONES = {
  aumentar_pct:{texto:'Aumentar un porcentaje',tipos:['numero']},
  redondear:{texto:'Redondear a múltiplos de',tipos:['numero']},
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
    : 'Valor<input id="m-valor" placeholder="' + (op === 'aumentar_pct' ? '15 para aumentar 15%' : op==='redondear'?'10 para múltiplos de $10':op === 'multiplicar' ? '1.15 para un 15%' : '') + '">';
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
    ESTADO.revision=r.plan._revision;ESTADO.requestId=null;
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
  ESTADO.archivo=files[0]||null;
  if (files[0]) $('#nombre-productos').textContent = files[0].name;
});

$('#btn-leer-productos').addEventListener('click', async () => {
  const texto = $('#pegar-productos').value.trim();
  if (texto) return previsualizar({ productos: leerTSV(texto), proveedores: [], precios: [] });
  const archivo = ESTADO.archivo;
  if (!archivo) return alert('Subí un archivo o pegá las celdas desde Excel.');
  try {
    // El .xlsx lo lee el servidor: trae las tres hojas de una. Va como cuerpo
    // crudo, igual que los PDF del agente administrativo.
    await previsualizar(await pedir(`/api/productos/leer?nombre=${encodeURIComponent(archivo.name)}`,
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
  if(ESTADO.busy)return;
  if(!confirm($('#accionbar-texto').textContent+'\nSe aplicará en Odoo producción. ¿Continuar?'))return;
  ESTADO.busy=true; ESTADO.requestId ||= crypto.randomUUID();
  const boton = $('#btn-aplicar');
  const textoOriginal = boton.textContent;
  boton.disabled = true; boton.textContent = 'Aplicando…';
  try {
    const r = ESTADO.hoja === 'imagenes'
      ? await pedir('/api/productos/imagenes', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ request_id:ESTADO.requestId, asignaciones: ESTADO.fotos.filter(f => f.sku)
            .map(f => ({ sku: f.sku, imagen: f.dataUrl })) }) })
      : await pedir('/api/productos/aplicar', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({...ESTADO.crudas,revision:ESTADO.revision,request_id:ESTADO.requestId}) });
    aviso('Carga #'+r.id+' recibida. Seguí su avance en el historial.');
    ESTADO.revision=null;
    await cargarHistorial();
  } catch (e) {
    boton.disabled = false; boton.textContent = textoOriginal;
    aviso('No se pudo confirmar el resultado: ' + e.message);
  } finally {ESTADO.busy=false;boton.textContent=textoOriginal;actualizarBarra();}
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

function aviso(texto){$('#mensaje-productos').textContent=texto;}
async function cargarHistorial(){
 try {
  const rows=await pedir('/api/productos/corridas');
  const box=$('#historial-cuerpo');box.replaceChildren();
  if(!rows.length){box.textContent='Todavía no hay cargas registradas.';return;}
  for(const r of rows){
   const card=document.createElement('article');card.className='card corrida';
   const title=document.createElement('h3');title.textContent=`Carga #${r.id} · ${r.tipo} · ${r.estado||'Anterior'}`;
   const info=document.createElement('p');info.textContent=`${new Date(r.cuando).toLocaleString('es-AR')} · ${r.avance||0} escrituras confirmadas. ${r.mensaje||''}`;
   card.append(title,info);
   for(const reg of r.registros||[]){
    try{const url=new URL(r.odoo_url);if(url.protocol!=='https:')continue;
     url.pathname='/web';url.hash=`id=${reg.id}&model=${reg.modelo}&view_type=form`;
     const link=document.createElement('a');link.href=url.href;link.target='_blank';link.rel='noopener';link.textContent=`Ver registro ${reg.id} ↗ `;card.append(link);
    }catch{}
   }
   if(['completada','parcial'].includes(r.estado)){
    const button=document.createElement('button');button.className='fantasma';button.textContent='Revisar reversión';
    button.onclick=async()=>{button.disabled=true;try{
     const plan=await pedir(`/api/productos/corridas/${r.id}/revertir?revisar=true`,{method:'POST'});
     if(confirm(plan.acciones.map(a=>`${a.accion} · registro ${a.id}`).join('\n')+'\n¿Confirmar reversión?')){
      await pedir(`/api/productos/corridas/${r.id}/revertir`,{method:'POST'});await cargarHistorial();
     }
    }catch(e){aviso(e.message);}finally{button.disabled=false;}};card.append(button);
   }
   box.append(card);
  }
 }catch(e){aviso(e.message);}
}
$('#btn-exportar').onclick=()=>{
 const lines=[['Hoja','SKU','Estado','Campo','Anterior','Nuevo','Errores']];
 for(const hoja of ['productos','proveedores','precios'])for(const r of ESTADO.filas[hoja]||[])
 for(const [col,value] of Object.entries(r.campos||{}))lines.push([hoja,r.sku,r.estado,col,value.viejo??'',value.nuevo??'',(r.errores||[]).join(' · ')]);
 const csv='\ufeff'+lines.map(row=>row.map(v=>'"'+String(v).replaceAll('"','""')+'"').join(';')).join('\r\n');
 const url=URL.createObjectURL(new Blob([csv],{type:'text/csv;charset=utf-8'}));const a=document.createElement('a');a.href=url;a.download='revision-productos.csv';a.click();URL.revokeObjectURL(url);
};
$('#btn-historial').onclick=cargarHistorial;
$('#solo-cambios').onchange=()=>pintarPreview('productos');
$$('[data-tarea]').forEach(button=>button.onclick=()=>{
 const task=button.dataset.tarea;
 if(['crear','modificar'].includes(task)){
  $('[data-tab="productos"]').click();$('#btn-otro-archivo').click();
  $(task==='crear'?'#productos-entrada':'#masiva').scrollIntoView({behavior:'smooth',block:'start'});
 }else{ $(`[data-tab="${task}"]`).click(); }
});
$('#btn-recuperar').onclick=()=>{try{const saved=JSON.parse(localStorage.getItem('kairon-productos-borrador')||'null');if(saved)previsualizar(saved);else aviso('No hay un borrador guardado.');}catch{aviso('No se pudo recuperar el borrador.');}};
async function refrescarPendientes(){try{ESTADO.pendientes=await pedir('/api/productos/pendientes');pintarPendientes();}catch(e){aviso(e.message);}}
$('#btn-revisar-pendientes').onclick=async()=>{try{await pedir('/api/productos/pendientes/revisar',{method:'POST'});await refrescarPendientes();aviso('Equivalencias comprobadas. Los comprobantes completos se revalidarán en Administración.');}catch(e){aviso(e.message);}};
setInterval(()=>{if(!document.hidden){cargarHistorial();if(ESTADO.hoja==='pendientes')refrescarPendientes();}},7000);
iniciar();cargarHistorial();
