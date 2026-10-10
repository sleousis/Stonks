// Tests for check-csp.mjs (run by `npm run lint`).
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { cspProblems, inlineHandlers, inlineScriptHashes } from './check-csp.mjs';

test('hashes inline scripts the way stonks.api.csp does', () => {
  // The same cases as tests/unit/test_api_csp.py.
  const html =
    '<script>a()</script>' +
    '<script src="main.js" type="module"></script>' +
    '<script type="application/json" id="d">{"x": 1}</script>' +
    '<script data-x>\nb()\n</script>';
  assert.deepEqual(inlineScriptHashes(html), [
    'sha256-qVpDBgj7bpq5hMAcGp3AOc79J3Y1Z4HvySTwKrWDoy4=',
    'sha256-NlXKTQe8YfLmZqnEm5aXK1Lb/1wWgkKgNs+lNRY6yOo=',
  ]);
});

test('passes a build that keeps the source scripts and adds only data and files', () => {
  const src = '<head><script>a()</script></head>';
  const built =
    '<head><script>a()</script><script type="application/json" id="route-preload">{}</script>' +
    '<link rel="stylesheet" href="styles.css"><script src="main.js" type="module"></script></head>';
  assert.deepEqual(cspProblems(src, built), []);
});

test('fails a build that changes or adds an inline script', () => {
  const src = '<script>a()</script>';
  assert.equal(cspProblems(src, '<script>a( )</script>').length, 1);
  assert.equal(cspProblems(src, '<script>a()</script><script>x()</script>').length, 1);
});

test('fails a build with an inline event handler', () => {
  const built = '<link rel="stylesheet" href="s.css" media="print" onload="this.media=\'all\'">';
  assert.deepEqual(inlineHandlers(built), ['onload']);
  assert.equal(cspProblems('', built).length, 1);
});
