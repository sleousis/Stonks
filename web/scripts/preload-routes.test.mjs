// Spec for the route preload step (UX-44). Run by `npm run lint`.
import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { test } from 'node:test';

import { chunkGraph, pattern, preloadScript, routeTable } from './preload-routes.mjs';

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
  const script = preloadScript(
    [{ re: '^/p/?$', modules: [resolve('/root', 'src/page.ts')] }],
    closure,
  );
  assert.match(script, /modulepreload/);
  assert.match(script, /"page\.js"/);
  assert.doesNotMatch(script, /core\.js|later\.js/);
});
