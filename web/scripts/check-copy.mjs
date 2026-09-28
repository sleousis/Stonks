// Fails when trader-facing copy sends people to a terminal or a config file
// (finding UI-09), or uses the system's names instead of trader words
// (UX-09, UX-49, docs/design/vocabulary.md): "tick", "ingest", "shadow",
// "promote", "register", class paths. "Retire" is a trader word now. Scans
// templates (.html) and string literals in .ts files under src/app, skipping
// comments, specs, styles and the generated client. Run by `npm run lint`.
//
//   node scripts/check-copy.mjs                 every file
//   node scripts/check-copy.mjs src/app/pages/lab   only files under these paths
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join, relative, sep } from 'node:path';
import { pathToFileURL } from 'node:url';

const ROOT = 'src/app';
const SKIP_DIRS = new Set(['generated']);

/** What a trader must never be asked to type or edit. Checked on all visible text. */
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

/**
 * System words with a trader word in the words table (docs/ui.md). Checked
 * on prose only: template text, visible attributes and string literals with
 * a space, after dropping paths and code-like tokens.
 */
export const WORD_RULES = [
  { id: 'tick', re: /\bticks?\b/i, say: 'Trading run' },
  { id: 'ingest', re: /\bingest(s|ed|ing|ion)?\b/i, say: 'Update data' },
  { id: 'shadow', re: /\bshadow\b/i, say: 'On trial, or Test book' },
  { id: 'promote', re: /\bpromot(e|es|ed|ing|ion)\b/i, say: 'Approve' },
  { id: 'register', re: /\bregist(er|ers|ered|ering|ration)\b/i, say: 'Put on trial' },
  { id: 'code', re: /\bclass_path\b|\bpython -m\b/i, say: 'a plain name' },
];

/**
 * Files allowed to use a system word, with the reason. Keep this short: a
 * page a trader can open is never on it.
 */
export const ALLOW = {
  // The glossary explains the system's names on purpose ("also called ...").
  'src/app/core/help/glossary.ts': ['tick', 'shadow', 'promote', 'retire', 'ingest', 'register'],
};

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

function stripCode(source) {
  return source.replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:'"`\\])\/\/.*$/gm, '$1');
}

const STRING_RE = /'(?:\\.|[^'\\\n])*'|"(?:\\.|[^"\\\n])*"|`(?:\\.|[^`\\])*`/g;

/** The text a trader can see: template text, or string literals outside comments. */
export function visibleText(source, isHtml) {
  if (isHtml) return stripHtmlComments(source);
  return (stripCode(source).match(STRING_RE) ?? []).join('\n');
}

/** Attributes whose static value is shown to people. */
const SHOWN_ATTRS =
  /\s(aria-label|title|placeholder|alt|label|hint|message|heading|description|subtitle|confirmLabel|emptyTitle|emptyText|emptyMessage|detail)\s*=\s*"([^"]*)"/g;

/** The words a person reads in a template: text between tags and shown attributes. */
export function templateProse(html) {
  let s = stripHtmlComments(html);
  s = s.replace(/\{\{[\s\S]*?\}\}/g, ' ');
  // Control flow headers: @if (...) {, @for (...), @let x = ...;, @case (...)
  s = s.replace(/@(if|else if|for|switch|case|defer|let)\b[^{;\n]*[{;]?/g, ' ');
  const shown = [];
  s = s.replace(/<[^>]*>/g, (tag) => {
    for (const m of tag.matchAll(SHOWN_ATTRS)) shown.push(m[2]);
    return ' ';
  });
  return `${s}\n${shown.join('\n')}`;
}

/** Drop what is code, not words: paths, dotted names, snake and kebab ids, placeholders. */
function scrub(text) {
  return text
    .replace(/\$\{[^}]*\}/g, ' ')
    .replace(/[\w.-]*\/[\w/{}:.?=&-]*/g, ' ')
    .replace(/\b\w+[._-]\w[\w.-]*/g, ' ');
}

/** Prose in a .ts file: inline templates as HTML, literals with a space, capitalised labels. */
export function tsProse(source) {
  const code = stripCode(source)
    .replace(/styles\s*:\s*`(?:\\.|[^`\\])*`/g, ' ')
    .replace(/styles\s*:\s*\[[\s\S]*?\]\s*,/g, ' ');
  const out = [];
  for (const lit of code.match(STRING_RE) ?? []) {
    const body = lit.slice(1, -1);
    if (lit[0] === '`' && /<[a-z][\w-]*[\s>]/i.test(body)) out.push(templateProse(body));
    else if (/\s/.test(body.trim()) && /[a-z]{3}/i.test(body)) out.push(body);
    // A capitalised single word is a label ('Shadow'); lowercase ones are keys.
    else if (/^[A-Z][a-z]+$/.test(body)) out.push(body);
  }
  return out.join('\n');
}

export function wordProblems(file, source) {
  const isHtml = file.endsWith('.html');
  const text = scrub(isHtml ? templateProse(source) : tsProse(source));
  const allowed = ALLOW[file.split(sep).join('/')] ?? [];
  const found = [];
  for (const rule of WORD_RULES) {
    if (allowed.includes(rule.id)) continue;
    const m = rule.re.exec(text);
    if (m) found.push(`${rule.id}: "${m[0]}" (say "${rule.say}")`);
  }
  return found;
}

function main(args) {
  const scopes = args.length ? args.map((a) => a.split(/[\\/]/).join(sep)) : null;
  const files = walk(ROOT).filter((f) => !scopes || scopes.some((s) => f.startsWith(s)));
  const problems = [];
  for (const file of files) {
    const source = readFileSync(file, 'utf8');
    const text = visibleText(source, file.endsWith('.html'));
    for (const rule of RULES) {
      const m = rule.re.exec(text);
      if (m) problems.push(`${relative('.', file)}: ${rule.id}: "${m[0]}"`);
    }
    for (const p of wordProblems(file, source)) problems.push(`${relative('.', file)}: ${p}`);
  }
  if (problems.length) {
    console.error(
      'Trader copy must use trader words, never the command line or config (UI-09, UX-49):',
    );
    for (const p of problems) console.error(`  ${p}`);
    process.exit(1);
  }
  console.log('Copy check passed: trader words only, no CLI or config-file language.');
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main(process.argv.slice(2));
}
