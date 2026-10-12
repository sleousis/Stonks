import type { Type } from '@angular/core';

/** A component's compiled styles as one string, for CSS rules a DOM test cannot see. */
export function componentCss(component: Type<unknown>): string {
  return (component as unknown as { ɵcmp: { styles: string[] } }).ɵcmp.styles.join('\n');
}

/**
 * The bodies of every rule whose selector mentions `selector`, so a spec can
 * say a class is never hidden (`display: none`) at any width.
 */
export function rulesFor(css: string, selector: string): string[] {
  const out: string[] = [];
  const re = /([^{}]+)\{([^{}]*)\}/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(css))) {
    // Drop Angular's scoping attributes and collapse spaces before matching.
    const sel = m[1]
      .replace(/\[_ng(content|host)-[^\]]*\]/g, '')
      .replace(/\s+/g, ' ')
      .trim();
    if (sel.includes(selector)) out.push(m[2].replace(/%NS%/g, ''));
  }
  return out;
}
