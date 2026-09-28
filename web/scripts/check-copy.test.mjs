// Spec for the copy check (UX-49). Run by `npm run lint` with `node --test`.
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { templateProse, tsProse, wordProblems } from './check-copy.mjs';

test('flags the old tick-confirm string', () => {
  const src = "const m = 'A real tick needs a reason.';";
  assert.match(wordProblems('src/app/x.ts', src).join(), /tick/);
});

test('flags a capitalised nav label but not a lowercase status key', () => {
  assert.match(wordProblems('src/app/x.ts', "{ label: 'Shadow' }").join(), /shadow/);
  assert.deepEqual(wordProblems('src/app/x.ts', "if (s.status === 'shadow') {}"), []);
});

test('ignores routes, API paths, snake ids and styles', () => {
  const src = [
    "router.navigate(['/shadow']);",
    "const url = 'GET /api/ticks failed for you';",
    "const k = 'shadow_pnl and more';",
    'styles: `.x { box-shadow: var(--shadow-1); }`,',
  ].join('\n');
  assert.deepEqual(wordProblems('src/app/x.ts', src), []);
});

test('reads template text and shown attributes, not bindings or control flow', () => {
  const html = `
    @if (s.status === 'shadow') { <span [status]="'shadow'">{{ s.shadow }}</span> }
    <app-status-pill status="active" />
    <input placeholder="Search runs" class="shadow" />`;
  assert.deepEqual(wordProblems('src/app/x.html', html), []);
  assert.match(templateProse('<p>Promotion checklist</p>'), /Promotion/);
  assert.match(templateProse('<button aria-label="Run a tick"></button>'), /tick/);
});

test('checks inline templates in components', () => {
  const src = 'template: `<h2>Shadow strategies</h2>`,';
  assert.match(tsProse(src), /Shadow strategies/);
});

test('lets Retire through: it is the vocabulary word for a strategy (vocabulary.md)', () => {
  assert.deepEqual(wordProblems('src/app/x.ts', "const label = 'Retire this strategy';"), []);
  assert.match(wordProblems('src/app/x.html', '<p>Promote it now</p>').join(), /Approve/);
});

test('honours the allowlist', () => {
  const src = "const d = 'Also called a tick.';";
  assert.deepEqual(wordProblems('src/app/core/help/glossary.ts', src), []);
});

test('flags model book, kill switch, incubating and go live (vocabulary.md)', () => {
  assert.match(
    wordProblems('src/app/x.html', '<p>The model book bought it</p>').join(),
    /Test book/,
  );
  assert.match(
    wordProblems('src/app/x.ts', "const m = 'Turn off the kill switch';").join(),
    /Stop trading/,
  );
  assert.match(
    wordProblems('src/app/x.ts', "const m = 'An incubating strategy';").join(),
    /On trial/,
  );
  assert.match(wordProblems('src/app/x.html', '<button>Go live</button>').join(), /Approve/);
  assert.match(
    wordProblems('src/app/x.html', '<p>3 paper trading strategies</p>').join(),
    /on trial/,
  );
  // The go-live check keeps its name, and "Going live" is the checklist page.
  assert.deepEqual(
    wordProblems('src/app/x.html', '<p>Go-live check. Going live checklist</p>'),
    [],
  );
});
