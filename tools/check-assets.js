/**
 * check-assets.js — pega esto en la consola del navegador, con la página abierta.
 *
 * Busca imágenes y vídeos rotos de tres formas, porque una sola no basta en
 * este sitio:
 *
 *   1. Lo que hay ahora mismo en el DOM. Solo ve el estado actual: las fotos de
 *      los tanatólogos y las carátulas cambian según avanzas, así que un
 *      escaneo suelto no las ve todas.
 *   2. Lo que index.html y support.js mencionan. Se descargan como texto y se
 *      extraen las rutas, igual que hace tools/optimize_media.py. Así no hay
 *      una lista que se quede vieja.
 *   3. Las series numeradas (foto-01, logo-02, VIDEO_03…). Se prueban una tras
 *      otra y se para tras varios fallos seguidos, para distinguir "falta la 7
 *      pero la 8 está" de "la serie se acabó en la 25".
 *
 * Comprueba con HEAD, así que NO descarga los vídeos.
 */
(async () => {
  const ORIGIN = location.origin;
  const CONCURRENCY = 8;

  // ---------------------------------------------------------------- utilidades
  const clean = (u) => {
    if (!u) return null;
    u = u.trim().split(/\s+/)[0];                    // srcset: "archivo 2x" → archivo
    if (!u || /^(data|blob|javascript):/i.test(u)) return null;
    // El <x-dc> conserva la plantilla sin resolver junto a lo ya renderizado.
    // Un "{{tanaFoto}}" no es un archivo roto, es un hueco que se rellena al
    // vuelo. Ojo: img.src devuelve la URL ya resuelta y codificada, donde las
    // llaves aparecen como %7B%7B, así que hay que mirar las dos formas.
    if (/\{\{|\}\}|%7B%7B|%7D%7D/i.test(u)) return null;
    try {
      const abs = new URL(u, location.href);
      if (abs.origin !== ORIGIN) return null;        // fuentes de Google, CDNs…
      return abs.pathname.replace(/^\//, '');
    } catch { return null; }
  };

  // Los nombres traen espacios, acentos y paréntesis: hay que codificarlos,
  // pero sin volver a codificar lo que ya venía codificado.
  const toURL = (p) => ORIGIN + '/' + p.split('/').map(s => {
    try { return encodeURIComponent(decodeURIComponent(s)); }
    catch { return encodeURIComponent(s); }
  }).join('/');

  async function probe(path) {
    const url = toURL(path);
    try {
      let r = await fetch(url, { method: 'HEAD', cache: 'no-store' });
      // Algunos servidores no responden bien a HEAD: reintenta pidiendo 1 byte.
      if (r.status === 405 || r.status === 501) {
        r = await fetch(url, { headers: { Range: 'bytes=0-0' }, cache: 'no-store' });
      }
      return {
        path, status: r.status, ok: r.ok,
        bytes: +(r.headers.get('content-length') || 0),
        type: r.headers.get('content-type') || '',
      };
    } catch (e) {
      return { path, status: 0, ok: false, bytes: 0, type: '', error: String(e.message || e) };
    }
  }

  async function pool(items, fn, n = CONCURRENCY) {
    const out = []; let i = 0;
    await Promise.all(Array.from({ length: Math.min(n, items.length) }, async () => {
      while (i < items.length) { const k = i++; out[k] = await fn(items[k]); }
    }));
    return out;
  }

  // ------------------------------------------------- 1. lo que está en el DOM
  const domRefs = new Set();
  const domBroken = [];

  for (const img of document.querySelectorAll('img')) {
    const p = clean(img.currentSrc || img.src);
    if (p) domRefs.add(p);
    // complete && naturalWidth === 0 es la señal fiable de "no pintó".
    if (img.complete && img.naturalWidth === 0 && (img.currentSrc || img.src)) {
      domBroken.push({ elemento: '<img>', url: clean(img.currentSrc || img.src) || img.src, alt: img.alt || '' });
    }
  }
  for (const s of document.querySelectorAll('source')) {
    const p = clean(s.getAttribute('srcset') || s.getAttribute('src'));
    if (p) domRefs.add(p);
  }
  for (const v of document.querySelectorAll('video')) {
    // preload="none" + data-src: el src real puede no estar puesto todavía.
    for (const a of ['src', 'data-src', 'poster']) {
      const p = clean(v.getAttribute(a));
      if (p) domRefs.add(p);
    }
    if (v.error) domBroken.push({ elemento: '<video>', url: v.currentSrc || v.getAttribute('data-src') || '', alt: 'error ' + v.error.code });
  }
  for (const el of document.querySelectorAll('*')) {
    const bg = getComputedStyle(el).backgroundImage;
    if (bg && bg !== 'none') {
      for (const m of bg.matchAll(/url\((['"]?)(.*?)\1\)/g)) {
        const p = clean(m[2]); if (p) domRefs.add(p);
      }
    }
  }

  // ------------------------------------- 2. lo que mencionan el HTML y el JS
  const srcRefs = new Set();
  const seriesSeeds = new Map();   // "prefijo|.ext" → true
  const MEDIA = 'png|jpe?g|webp|avif|gif|svg|mp4|webm|mov';

  for (const file of ['index.html', 'support.js']) {
    let text;
    try { text = await (await fetch(file, { cache: 'no-store' })).text(); } catch { continue; }

    // rutas literales completas
    for (const m of text.matchAll(new RegExp(`(?:assets|uploads)/[^\\s"'()<>{}\\\\]+?\\.(?:${MEDIA})`, 'gi')))
      srcRefs.add(m[0]);

    // nombres sueltos en listas JS, unidos a un prefijo en otra línea
    // Debe empezar por alfanumérico: así '-poster.jpg' y '.webp', que salen de
    // los .replace() que derivan rutas en el JS, no se toman por archivos.
    const bare = [...text.matchAll(new RegExp(`['"]([A-Za-z0-9_][^'"/\\\\]*\\.(?:${MEDIA}))['"]`, 'gi'))].map(m => m[1]);
    const prefixes = [...new Set([...text.matchAll(/['"]((?:assets|uploads)\/(?:[A-Za-z0-9_-]+\/)*)['"]\s*\+/g)].map(m => m[1]))]
      .concat(['uploads/higgsfield/', 'uploads/', 'assets/testimonios/', 'assets/'])
      .sort((a, b) => b.split('/').length - a.split('/').length);   // más específico primero
    for (const name of new Set(bare))
      for (const pre of prefixes) srcRefs.add(pre + name);          // se filtra al probar

    // series: 'assets/tanatologos/foto-' + nn + '.jpg'
    for (const m of text.matchAll(/['"]((?:assets|uploads)\/[A-Za-z0-9_/-]*?)['"]\s*\+\s*\w+\s*\+\s*['"](\.[a-z0-9]+)['"]/gi))
      seriesSeeds.set(m[1] + '|' + m[2], true);
  }

  // ------------------------------------------------- 3. recorrer las series
  async function walkSeries(prefix, ext) {
    const hits = [], gaps = [];
    let misses = 0;
    for (let n = 1; n <= 99 && misses < 4; n++) {
      const pad = String(n).padStart(2, '0');
      const r = await probe(`${prefix}${pad}${ext}`);
      if (r.ok) { hits.push(n); misses = 0; }
      else { gaps.push({ n, r }); misses++; }
    }
    // Solo son huecos de verdad los anteriores al último que sí existe.
    const last = hits.length ? Math.max(...hits) : 0;
    return { ok: hits.length, gaps: gaps.filter(g => g.n < last).map(g => g.r) };
  }

  console.log('%cRevisando assets…', 'font-weight:bold');
  const seriesBroken = [];
  const seriesPrefixes = new Set();
  let seriesOk = 0;
  for (const key of seriesSeeds.keys()) {
    const [prefix, ext] = key.split('|');
    seriesPrefixes.add(prefix);
    const r = await walkSeries(prefix, ext);
    seriesOk += r.ok;
    seriesBroken.push(...r.gaps);
  }

  // ------------------------------------------------------------- 4. probar todo
  const candidates = [...new Set([...domRefs, ...srcRefs])]
    // las series ya se recorrieron aparte
    .filter(p => ![...seriesPrefixes].some(pre => p.startsWith(pre)));

  const results = await pool(candidates, probe);

  // Un nombre suelto se probó con varios prefijos a propósito; si alguno
  // acertó, los demás son ruido, no archivos rotos.
  const okByName = new Set(results.filter(r => r.ok).map(r => r.path.split('/').pop()));
  const broken = results
    .filter(r => !r.ok && !okByName.has(r.path.split('/').pop()))
    .concat(seriesBroken);

  // --------------------------------------------------------------- 5. informe
  const okCount = results.filter(r => r.ok).length + seriesOk;
  console.log(
    `%c${okCount} ok  ·  ${broken.length} rotos  ·  ${domBroken.length} sin pintar en el DOM`,
    `font-weight:bold;color:${broken.length || domBroken.length ? '#c0392b' : '#27ae60'}`
  );

  if (broken.length) {
    console.group('%c✗ No responden (404 o error de red)', 'color:#c0392b;font-weight:bold');
    console.table(broken.map(r => ({ archivo: r.path, estado: r.status || r.error })));
    console.groupEnd();
  }
  if (domBroken.length) {
    console.group('%c⚠ En el DOM pero sin pintar', 'color:#e67e22;font-weight:bold');
    console.log('Si la URL responde 200, suele ser un <source> de <picture> que falla, ' +
                'o un archivo con extensión que no coincide con su contenido.');
    console.table(domBroken);
    console.groupEnd();
  }
  if (!broken.length && !domBroken.length) console.log('%c✓ Todo responde.', 'color:#27ae60');

  const pesado = results.filter(r => r.ok && r.bytes > 1.5e6).sort((a, b) => b.bytes - a.bytes);
  if (pesado.length) {
    console.group(`%c⚖ ${pesado.length} archivos por encima de 1.5 MB`, 'color:#2980b9;font-weight:bold');
    console.table(pesado.slice(0, 20).map(r => ({ archivo: r.path, MB: (r.bytes / 1048576).toFixed(2), tipo: r.type })));
    console.groupEnd();
  }

  // Extensión que no concuerda con el Content-Type que sirve el servidor.
  const mismatch = results.filter(r => {
    if (!r.ok || !r.type) return false;
    const ext = (r.path.split('.').pop() || '').toLowerCase();
    const t = r.type.toLowerCase();
    return (ext === 'png' && !t.includes('png')) ||
           ((ext === 'jpg' || ext === 'jpeg') && !t.includes('jpeg')) ||
           (ext === 'webp' && !t.includes('webp'));
  });
  if (mismatch.length) {
    console.group('%c⚠ La extensión no coincide con el tipo servido', 'color:#e67e22;font-weight:bold');
    console.table(mismatch.map(r => ({ archivo: r.path, 'content-type': r.type })));
    console.groupEnd();
  }

  return { ok: okCount, rotos: broken, sinPintar: domBroken };
})();
