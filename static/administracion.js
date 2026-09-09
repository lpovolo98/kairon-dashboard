let accessToken = ''; // Memory only; normal portal access uses verified Cloudflare identity.
let ready = false;
let selected = null;
const $ = id => document.getElementById(id);
async function api(path, options = {}) {
  const headers = { Accept: 'application/json', ...(options.headers || {}) };
  if (accessToken) headers.Authorization = 'Bearer ' + accessToken;
  let response;
  const uncertain = options.method === 'POST' ? ' Antes de repetir la carga, actualizá el historial para comprobar si fue recibida.' : '';
  try {
    response = await fetch('/api/administracion/' + path, {...options, headers, credentials: 'same-origin', redirect: 'manual'});
  } catch {
    throw new Error('No se pudo conectar con el portal.' + uncertain);
  }
  if (response.type === 'opaqueredirect' || response.status === 401 || response.status === 403) {
    throw new Error('Tu acceso necesita verificación. Volvé a ingresar al portal y actualizá el historial.' + uncertain);
  }
  const type = response.headers.get('content-type') || '';
  if (!type.includes('application/json')) {
    const reason = response.status >= 500
      ? 'El servicio no está disponible en este momento.'
      : 'El portal devolvió una página en lugar del resultado. Volvé a ingresar al portal y actualizá el historial.';
    throw new Error(reason + uncertain);
  }
  let data;
  try { data = await response.json(); }
  catch { throw new Error('La respuesta del servicio llegó incompleta.' + uncertain); }
  if (!response.ok) throw new Error(data.detail || 'No se pudo completar la operación');
  return data;
}
function choose(file) {
  selected = file;
  $('filename').textContent = file ? file.name : 'Arrastrá tu comprobante acá';
  $('submit').disabled = !ready || !file;
}
$('pdf').addEventListener('change', event => choose(event.target.files[0]));
for (const eventName of ['dragover', 'dragenter']) $('drop').addEventListener(eventName, event => {
  event.preventDefault(); $('drop').classList.add('dragging');
});
$('drop').addEventListener('dragleave', () => $('drop').classList.remove('dragging'));
$('drop').addEventListener('drop', event => {
  event.preventDefault(); $('drop').classList.remove('dragging');
  if (event.dataTransfer.files.length !== 1) { $('notice').textContent = 'Seleccioná un solo PDF por carga.'; return; }
  choose(event.dataTransfer.files[0]);
});
$('upload').noValidate = true;
$('upload').addEventListener('submit', async event => {
  event.preventDefault();
  if (!selected || !ready) return;
  if (!/\.pdf$/i.test(selected.name) || selected.size > 15 * 1024 * 1024) {
    $('notice').textContent = 'Seleccioná un PDF de hasta 15 MB.'; return;
  }
  $('submit').disabled = true;
  $('notice').textContent = 'Enviando comprobante…';
  try {
    await api('facturas', {method: 'POST', headers: {'Content-Type': 'application/pdf'}, body: selected});
    $('notice').textContent = 'Comprobante recibido. Podés seguir el avance en el historial.';
    choose(null); $('pdf').value = ''; await history();
  } catch (error) { $('notice').textContent = error.message; }
  finally { $('submit').disabled = !ready || !selected; }
});
const labels = {recibido:'Recibida', leyendo:'Leyendo PDF', validando:'Controlando', creando_orden:'Creando orden', creando_factura:'Cargando', verificando:'Verificando', contabilizando:'Contabilizando', completado:'Contabilizada', borrador:'En borrador', existente:'Ya existe en Odoo', revision:'Requiere revisión', resultado_incierto:'Revisar en Odoo'};
labels.creando_proveedor = 'Creando proveedor'; labels.confirmando_orden = 'Confirmando compra';
labels.esperando_productos='Esperando productos';
async function history() {
  try {
    const rows = await api('facturas');
    $('history').replaceChildren();
    if (!rows.length) {
      const empty = document.createElement('p'); empty.className = 'empty';
      empty.textContent = 'Todavía no cargaste facturas. Tu primera carga aparecerá acá.';
      $('history').append(empty);
    }
    for (const row of rows) {
      const item = document.createElement('article'); item.className = 'invoice';
      const text = document.createElement('div');
      const title = document.createElement('b');
      title.textContent = row.document ? row.document.proveedor.nombre : 'Factura recibida';
      const subtitle = document.createElement('p');
      subtitle.textContent = row.document ? (row.document.comprobante.tipo === 'NOTA_CREDITO' ? 'Nota de crédito · ' : row.document.comprobante.clase === 'interno' ? 'Compra interna · ' : 'Factura · ') + row.document.comprobante.numero + ' · ' + new Intl.NumberFormat('es-AR', {style:'currency',currency:'ARS'}).format(row.document.totales.total) : new Date(row.created_at).toLocaleString('es-AR');
      const message = document.createElement('p'); message.textContent = row.message;
      text.append(title, subtitle, message);
      if (row.document) {
        const d = row.document;
        const details = document.createElement('details');
        details.open = true;
        const summary = document.createElement('summary'); summary.textContent = 'Datos leídos del PDF';
        const content = document.createElement('pre');
        content.textContent = [
          'CUIT proveedor: ' + d.proveedor.cuit,
          'CUIT receptor: ' + d.comprador_cuit,
          'Fecha: ' + d.comprobante.fecha + ' · Vencimiento: ' + (d.comprobante.fecha_vencimiento || 'No indicado'),
          ...(d.comprobante.cae ? ['CAE: ' + d.comprobante.cae + ' · Vence: ' + (d.comprobante.cae_vencimiento || 'No indicado')] : ['Comprobante sin validez fiscal']),
          ...(d.comprobante.factura_original ? ['Factura original: ' + d.comprobante.factura_original, 'Diferencia de precio · Sin movimiento de stock'] : []),
          '', ...d.lineas.map(l => l.codigo_proveedor + ' · ' + l.descripcion + '\nCantidad: ' + (l.cantidad_unidades ?? l.cantidad) + ' · Precio: $' + l.precio_unitario.toLocaleString('es-AR') + ' · Descuento: ' + (l.bonificacion_pct || 0) + '% · Neto: $' + l.subtotal.toLocaleString('es-AR')),
          '', 'Neto: $' + d.totales.neto.toLocaleString('es-AR') + ' · IVA: $' + d.totales.iva.toLocaleString('es-AR'),
          'Total: $' + d.totales.total.toLocaleString('es-AR'),
          ...(d.observaciones_carga ? [d.observaciones_carga] : []),
          ...(row.existing_state ? ['Estado actual en Odoo: ' + (row.existing_state === 'posted' ? 'Contabilizada' : row.existing_state)] : [])
        ].join('\n');
        details.append(summary, content); text.append(details);
      }
      if (row.purchase_id) { const po = document.createElement('small'); po.textContent = 'Orden de compra ID ' + row.purchase_id; text.append(po); }
      if (row.report) {
        const details = document.createElement('details'); const summary = document.createElement('summary');
        summary.textContent = 'Ver controles'; const report = document.createElement('pre'); report.textContent = row.report.join('\n');
        details.append(summary, report); text.append(details);
      }
      const state = document.createElement('span'); state.className = 'badge ' + (row.state === 'completado' ? 'success' : ''); state.textContent = labels[row.state] || row.state;
      const actions = document.createElement('div'); actions.className = 'invoice-actions'; actions.append(state);
      if (row.odoo_url && new URL(row.odoo_url, location.origin).protocol === 'https:') {
        const link = document.createElement('a'); link.href = row.odoo_url; link.target = '_blank'; link.rel = 'noopener'; link.textContent = 'Abrir en Odoo ↗'; actions.append(link);
      }
      for (const [url,label] of [[row.purchase_url, 'Ver compra ' + (row.purchase_name || '') + ' ↗'], ...(row.picking_urls || []).map(url=>[url,'Ver recepción pendiente ↗'])]) {
        if (url && new URL(url, location.origin).protocol === 'https:') {
          const link=document.createElement('a'); link.href=url; link.target='_blank'; link.rel='noopener'; link.textContent=label; actions.append(link);
        }
      }
      item.append(text, actions); $('history').append(item);
    }
  } catch (error) { $('notice').textContent = error.message; }
}
async function init() {
  try {
    const status = await api('status'); ready = status.ready;
    $('connection').textContent = ready ? '● Odoo producción · Comprobantes en borrador · Recepciones pendientes' + (status.reader !== 'general' ? ' · Formatos habilitados: Cibos, Starbread y presupuesto Almadre confirmado.' : '') : 'Pendiente de configuración para comenzar a cargar comprobantes';
    $('connection').classList.toggle('online', ready);
    $('submit').disabled = !ready || !selected;
    await history();
  } catch (error) { ready = false; $('submit').disabled = true; $('connection').textContent = error.message; }
}
$('login').addEventListener('submit', event => { event.preventDefault(); accessToken = $('token').value; $('token').value = ''; init(); });
$('refresh').addEventListener('click', init);
setInterval(() => { if (ready && !document.hidden) history(); }, 5000);
init();
