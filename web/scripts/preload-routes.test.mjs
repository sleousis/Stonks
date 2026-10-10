// Spec for the route preload step (UX-44). Run by `npm run lint`.
import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { test } from 'node:test';

import {
  chunkGraph,
  injectPreload,
  pattern,
  preloadData,
  routeTable,
} from './preload-routes.mjs';

test('turns route paths into anchored patterns', () => {
  assert.equal(pattern(['']), '^/$');
  assert.equal(pattern(['strategies', ':id']), '^/strategies/[^/]+/?$');
  assert.equal(pattern(['**']), '^/.*$');
});

test('follows loadChildren files and inline children in router order', () => {
  const dir = mkdtempSync(join(tmpdir(), 'routes-'));
  writeFileSync(
    join(dir, 'app.routes.ts'),
    `export const routes = [
      { path: 'login', data: { public: true }, loadChildren: () => import('./login.routes') },
      { path: 'orders', loadChildren: () => import('./orders.routes') },
      { path: '**', loadComponent: () => import('./not-found').then((m) => m.X) },
    ];`,
  );
  writeFileSync(
    join(dir, 'login.routes.ts'),
    `export default [{ path: '', loadComponent: () => import('./login.page') }];`,
  );
  writeFileSync(
    join(dir, 'orders.routes.ts'),
    `export default [{
      path: '',
      loadComponent: () => import('./orders.page'),
      children: [
        { path: '', loadComponent: () => import('./list.page') },
        { path: 'ticks/:id', loadComponent: () => import('./tick.page') },
      ],
    }];`,
  );
  const rows = routeTable(join(dir, 'app.routes.ts'));
  assert.deepEqual(
    rows.map((r) => r.re),
    ['^/login/?$', '^/orders/?$', '^/orders/ticks/[^/]+/?$', '^/.*$'],
  );
  const tick = rows[2].modules.map((m) => m.split(/[\\/]/).pop());
  assert.deepEqual(tick, ['orders.routes.ts', 'orders.page.ts', 'tick.page.ts']);
});

test('preloads a route chunk and its static imports, never the initial ones', () => {
  const stats = {
    outputs: {
      'main-A.js': { imports: [{ path: 'core.js', kind: 'import-statement' }] },
      'core.js': { imports: [] },
      'page.js': {
        entryPoint: 'src/page.ts',
        imports: [
          { path: 'core.js', kind: 'import-statement' },
          { path: 'shared.js', kind: 'import-statement' },
          { path: 'later.js', kind: 'dynamic-import' },
        ],
      },
      'shared.js': { imports: [] },
      'later.js': { entryPoint: 'src/later.ts', imports: [] },
    },
  };
  const { closure, eager } = chunkGraph(stats, '/root');
  assert.deepEqual(eager, ['core.js']);
  assert.deepEqual(closure(resolve('/root', 'src/page.ts')).sort(), ['page.js', 'shared.js']);
  const data = preloadData(
    [{ re: '^/p/?$', modules: [resolve('/root', 'src/page.ts')] }],
    closure,
  );
  assert.match(data, /^<script type="application\/json" id="route-preload" data-route-preload>/);
  assert.match(data, /"page\.js"/);
  assert.doesNotMatch(data, /core\.js|later\.js/);
});

test('the route table is data, never script, so the CSP hash stays fixed', () => {
  const data = preloadData([{ re: '^/a</script>/?$', modules: ['m'] }], () => ['x.js']);
  const json = data.replace(/^<script[^>]*>/i, '').replace(/<\/script\s*>$/i, '');
  assert.doesNotMatch(json, /</);
  assert.deepEqual(JSON.parse(json), { n: ['x.js'], r: [['^/a</script>/?$', [0]]] });
});

const RUN = '<script data-route-preload-run>\n  run();\n</script>';

test('writes the table and the links just before the preload loop', () => {
  const html = `<head><meta charset="utf-8">${RUN}</head>`;
  const data = '<script type="application/json" id="route-preload" data-route-preload>{}</script>';
  const links = '<link rel="modulepreload" href="a.js" data-route-preload>';
  const once = injectPreload(html, links, data);
  assert.equal(once, `<head><meta charset="utf-8">${links}${data}${RUN}</head>`);
  // Run twice (a rebuild over the same output): no duplicates.
  assert.equal(injectPreload(once, links, data), once);
  // The Angular build writes the bare attribute as ="".
  const built = RUN.replace('-run>', '-run="">');
  assert.equal(injectPreload(built, links, data), `${links}${data}${built}`);
});

test('refuses an index.html without the preload loop', () => {
  assert.throws(() => injectPreload('<head></head>', '', ''), /data-route-preload-run/);
});
