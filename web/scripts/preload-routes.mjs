// Route-level module preload (UX-44). Run after `ng build` by `npm run build`.
//
// Each page is a lazy chunk that imports more shared chunks, so on a cold
// load the browser finds them one round trip at a time. This reads the
// build's stats, maps every route to the chunks it needs, and writes a small
// inline script into index.html that adds <link rel="modulepreload"> for the
// current URL's chunks, so they download in parallel with main.js.
//
// Also refreshes index.html's hash in ngsw.json (the service worker checks it)
// and removes the stats file from the output.
import { createHash } from 'node:crypto';
import { existsSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { dirname, join, relative, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const DIST = 'dist';
const STATS = join(DIST, 'browser-stats.json');
const INDEX = join(DIST, 'index.html');
const NGSW = join(DIST, 'ngsw.json');
const MARK = 'data-route-preload';

/** The `{ ... }` object literal around `at`, by brace matching. */
function enclosingObject(src, at) {
  let depth = 0;
  let start = -1;
  for (let i = at; i >= 0; i--) {
    const c = src[i];
    if (c === '}') depth++;
    else if (c === '{') {
      if (depth === 0) {
        start = i;
        break;
      }
      depth--;
    }
  }
  if (start < 0) return null;
  depth = 0;
  for (let i = start; i < src.length; i++) {
    const c = src[i];
    if (c === '{') depth++;
    else if (c === '}' && --depth === 0)
      return { start, end: i + 1, text: src.slice(start, i + 1) };
  }
  return null;
}

/** Text of an object literal at its own depth only (nested objects blanked). */
function topLevel(obj) {
  let out = '';
  let depth = 0;
  for (const c of obj) {
    if (c === '{') depth++;
    if (depth <= 1) out += c;
    if (c === '}') depth--;
  }
  return out;
}

/**
 * Routes declared in a file, as a tree in source order: `{ path, kind,
 * target, children }`. `target` is the imported module's path without
 * extension. Inline `children` of a route become its children here.
 */
export function parseRoutes(file) {
  const src = readFileSync(file, 'utf8');
  const nodes = [];
  const re = /(loadChildren|loadComponent)\s*:\s*\(\)\s*=>\s*import\(\s*'([^']+)'\s*\)/g;
  for (const m of src.matchAll(re)) {
    const obj = enclosingObject(src, m.index);
    if (!obj) continue;
    const path = /\bpath\s*:\s*'([^']*)'/.exec(topLevel(obj.text))?.[1];
    if (path === undefined) continue;
    nodes.push({
      path,
      kind: m[1],
      target: resolve(dirname(file), m[2]),
      start: obj.start,
      end: obj.end,
      children: [],
    });
  }
  const roots = [];
  for (const node of nodes) {
    const parent = nodes
      .filter((o) => o !== node && o.start < node.start && node.end <= o.end)
      .sort((a, b) => b.start - a.start)[0];
    (parent ? parent.children : roots).push(node);
  }
  return roots;
}

/** A leaf route's path as a regular expression over `location.pathname` (a leaf matches the whole URL). */
export function pattern(segments) {
  const parts = segments.filter(Boolean).join('/');
  if (parts === '**' || parts.endsWith('/**')) {
    const head = parts.replace(/\/?\*\*$/, '');
    return head ? `^/${head.replace(/:[^/]+/g, '[^/]+')}(/.*)?$` : '^/.*$';
  }
  const body = parts.replace(/:[^/]+/g, '[^/]+');
  return `^/${body}${body ? '/?' : ''}$`;
}

/** Every route leaf with the module files it loads, in router order. */
export function routeTable(file, prefix = [], nodes = parseRoutes(file)) {
  const rows = [];
  for (const r of nodes) {
    const segs = [...prefix, r.path];
    const target = `${r.target}.ts`;
    const below =
      r.kind === 'loadChildren'
        ? routeTable(target, segs)
        : r.children.length
          ? routeTable(file, segs, r.children)
          : [{ re: pattern(segs), modules: [] }];
    for (const child of below) rows.push({ ...child, modules: [target, ...child.modules] });
  }
  return rows;
}

/** Chunks each module needs: its entry chunk and every chunk it imports statically. */
export function chunkGraph(stats, root) {
  const byEntry = new Map();
  const imports = new Map();
  const initial = new Set();
  for (const [file, out] of Object.entries(stats.outputs)) {
    if (!file.endsWith('.js')) continue;
    const name = file.split('/').pop();
    imports.set(
      name,
      (out.imports ?? [])
        .filter((i) => i.kind === 'import-statement')
        .map((i) => i.path.split('/').pop()),
    );
    if (out.entryPoint) byEntry.set(resolve(root, out.entryPoint), name);
  }
  const main = [...imports.keys()].find((n) => n.startsWith('main-'));
  const walk = (name, seen) => {
    if (!name || seen.has(name)) return seen;
    seen.add(name);
    for (const dep of imports.get(name) ?? []) walk(dep, seen);
    return seen;
  };
  walk(main, initial);
  const closure = (module) => {
    const entry = byEntry.get(module);
    return entry ? [...walk(entry, new Set())].filter((n) => !initial.has(n)) : [];
  };
  const eager = [...initial].filter((n) => n !== main);
  return { closure, eager };
}

/** The inline script: a chunk list, route patterns with chunk indexes, and the loop. */
export function preloadScript(rows, closure) {
  const names = [];
  const index = new Map();
  const table = [];
  for (const row of rows) {
    const chunks = new Set(row.modules.flatMap(closure));
    if (!chunks.size) continue;
    const ids = [...chunks].map((n) => {
      if (!index.has(n)) index.set(n, names.push(n) - 1);
      return index.get(n);
    });
    table.push([row.re, ids]);
  }
  const data = JSON.stringify({ n: names, r: table });
  return (
    `<script ${MARK}>(function(){try{var d=${data},p=location.pathname,h=document.head;` +
    `for(var i=0;i<d.r.length;i++){if(new RegExp(d.r[i][0]).test(p)){` +
    `d.r[i][1].forEach(function(j){var l=document.createElement('link');` +
    `l.rel='modulepreload';l.href=d.n[j];h.appendChild(l)});break}}}catch(e){}})()</script>`
  );
}

function sha1(path) {
  return createHash('sha1').update(readFileSync(path)).digest('hex');
}

function main() {
  if (!existsSync(STATS)) {
    console.error(`preload-routes: ${STATS} is missing (the production build writes it).`);
    process.exit(1);
  }
  const stats = JSON.parse(readFileSync(STATS, 'utf8'));
  const { closure, eager } = chunkGraph(stats, process.cwd());
  const rows = routeTable(resolve('src/app/app.routes.ts'));
  const script = preloadScript(rows, closure);
  // The initial chunks main.js imports: fetched with main, not after it.
  const links = eager.map((n) => `<link rel="modulepreload" href="${n}" ${MARK}>`).join('');
  let html = readFileSync(INDEX, 'utf8')
    .replace(new RegExp(`<script ${MARK}>.*?</script>`), '')
    .replace(new RegExp(`<link rel="modulepreload" href="[^"]+" ${MARK}>`, 'g'), '');
  html = html.replace('</head>', `${links}${script}\n</head>`);
  writeFileSync(INDEX, html);
  if (existsSync(NGSW)) {
    const ngsw = JSON.parse(readFileSync(NGSW, 'utf8'));
    if (ngsw.hashTable?.['/index.html']) {
      ngsw.hashTable['/index.html'] = sha1(INDEX);
      writeFileSync(NGSW, JSON.stringify(ngsw, null, 2));
    }
  }
  rmSync(STATS);
  console.log(
    `preload-routes: ${rows.length} routes, script ${script.length} bytes in ${relative('.', INDEX)}`,
  );
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) main();
