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

/** The text a trader can see: template text, or string literals outside comments. */
export function visibleText(source, isHtml) {
  if (isHtml) {
    // Strip until stable, so a comment hidden inside another cannot survive.
    let text = source;
    let prev;
    do {
      prev = text;
      text = text.replace(/<!--[\s\S]*?-->/g, '');
    } while (text !== prev);
    return text.replace(/<!--|-->/g, '');
  }
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
