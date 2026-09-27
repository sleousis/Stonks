// Fails when trader-facing copy sends people to a terminal or a config file
// (finding UI-09). Scans templates (.html) and string literals in .ts files
// under src/app, skipping comments, specs and the generated client.
// Run by `npm run lint`.
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join, relative } from 'node:path';

const ROOT = 'src/app';
const SKIP_DIRS = new Set(['generated']);

/** What a trader must never be asked to type or edit. */
export const RULES = [
  {
    id: 'cli',
    re: /\bstonks\s+(serve|tick|lab|ingest|audit|golive|db|universe|registry|halts|backup|schedule|health|pnl|report|mcp)\b/i,
  },
  { id: 'env', re: /\bSTONKS_[A-Z_]+/ },
  {
    id: 'config',
    re: /\[(api|lab|production|golive|studio|risk|notify|ingest|brokers?|scheduler|ops)(\.[a-z_]+)*\]/,
  },
  { id: 'terminal', re: /\b(command line|from a terminal|in a terminal)\b/i },
  { id: 'toml', re: /\b[a-z_]+\.toml\b/ },
];

function walk(dir, out = []) {
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) {
      if (!SKIP_DIRS.has(name)) walk(path, out);
    } else if (/\.(html|ts)$/.test(name) && !/\.spec\.ts$/.test(name) && !/\.gen\.ts$/.test(name)) {
      out.push(path);
    }
  }
  return out;
}

/**
 * Drop HTML comments with a plain scan (no regex): a comment runs from
 * `<!--` to the first `-->` or `--!>`, or to the end of the file.
 */
export function stripHtmlComments(source) {
  let out = '';
  let i = 0;
  for (;;) {
    const open = source.indexOf('<!--', i);
    if (open === -1) return out + source.slice(i);
    out += source.slice(i, open);
    const ends = ['-->', '--!>']
      .map((end) => [source.indexOf(end, open + 4), end.length])
      .filter(([at]) => at !== -1);
    if (ends.length === 0) return out;
    const [at, len] = ends.reduce((a, b) => (b[0] < a[0] ? b : a));
    i = at + len;
  }
}

/** The text a trader can see: template text, or string literals outside comments. */
export function visibleText(source, isHtml) {
  if (isHtml) return stripHtmlComments(source);
  const code = source.replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:'"`])\/\/.*$/gm, '$1');
  const strings = code.match(/'(?:\\.|[^'\\\n])*'|"(?:\\.|[^"\\\n])*"|`(?:\\.|[^`\\])*`/g) ?? [];
  return strings.join('\n');
}

const problems = [];
for (const file of walk(ROOT)) {
  const text = visibleText(readFileSync(file, 'utf8'), file.endsWith('.html'));
  for (const rule of RULES) {
    const m = rule.re.exec(text);
    if (m) problems.push(`${relative('.', file)}: ${rule.id}: "${m[0]}"`);
  }
}

if (problems.length) {
  console.error('Trader copy must not mention the command line or config files (UI-09):');
  for (const p of problems) console.error(`  ${p}`);
  process.exit(1);
}
console.log('Copy check passed: no CLI or config-file language in the console.');
