// Fails the build when the built index.html would break the
// Content-Security-Policy. Run last by `npm run build`.
//
// The policy allows the inline scripts of src/index.html by their sha256
// hash (src/stonks/api/csp.py, checked against src/index.html by
// tests/unit/test_api_csp.py). This check closes the loop on the build
// side: the built page must run exactly those inline scripts, byte for
// byte, and no inline event handler (onload=...), which the policy blocks.
// A new Angular version that injects a script of its own fails here, not
// in the browser.
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';

const EXECUTABLE = new Set(['', 'text/javascript', 'application/javascript', 'module']);

function attribute(attrs, name) {
  const m = new RegExp(`\\b${name}\\s*=\\s*("[^"]*"|'[^']*'|[^\\s>]+)`, 'i').exec(attrs);
  return m ? m[1].replace(/^["']|["']$/g, '') : null;
}

/** `sha256-...` for each inline script the browser would run (as in stonks.api.csp). */
export function inlineScriptHashes(html) {
  const out = [];
  for (const m of html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/gi)) {
    const [, attrs, body] = m;
    if (attribute(attrs, 'src') !== null) continue;
    if (!EXECUTABLE.has((attribute(attrs, 'type') ?? '').toLowerCase())) continue;
    out.push(`sha256-${createHash('sha256').update(body, 'utf8').digest('base64')}`);
  }
  return out;
}

/** Inline event handler attributes (onload=, onclick=, ...) in the page's tags. */
export function inlineHandlers(html) {
  return [...html.matchAll(/<[a-z][^>]*\s(on[a-z]+)\s*=/gi)].map((m) => m[1]);
}

/** Problems that would make the built page break under the policy. */
export function cspProblems(sourceHtml, builtHtml) {
  const problems = [];
  const want = inlineScriptHashes(sourceHtml);
  const got = inlineScriptHashes(builtHtml);
  if (JSON.stringify(want) !== JSON.stringify(got)) {
    problems.push(
      `inline scripts differ: src/index.html has ${JSON.stringify(want)}, the build ${JSON.stringify(got)}`,
    );
  }
  for (const handler of inlineHandlers(builtHtml)) {
    problems.push(`inline event handler ${handler}= (blocked by the policy)`);
  }
  return problems;
}

function main() {
  const problems = cspProblems(
    readFileSync('src/index.html', 'utf8'),
    readFileSync('dist/index.html', 'utf8'),
  );
  if (problems.length) {
    for (const p of problems) console.error(`check-csp: ${p}`);
    console.error('check-csp: see src/stonks/api/csp.py for how the hashes are kept.');
    process.exit(1);
  }
  console.log('check-csp: the built index.html matches the policy');
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) main();
