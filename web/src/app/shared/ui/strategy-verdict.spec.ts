import { TestBed } from '@angular/core/testing';

import type { Verdict } from '../strategy-verdict';
import { StrategyVerdict } from './strategy-verdict';

function verdict(reasons: string[], level: Verdict['level'] = 'promising'): Verdict {
  const words = {
    worth: 'Worth following',
    promising: 'Promising, needs more data',
    not_yet: 'Not good enough yet',
  } as const;
  const tones = { worth: 'positive', promising: 'warn', not_yet: 'negative' } as const;
  return { level, label: words[level], tone: tones[level], reasons };
}

describe('StrategyVerdict (F33)', () => {
  function render(v: Verdict, opts: { compact?: boolean; details?: boolean } = {}): HTMLElement {
    const fixture = TestBed.createComponent(StrategyVerdict);
    fixture.componentRef.setInput('verdict', v);
    if (opts.compact) fixture.componentRef.setInput('compact', true);
    if (opts.details) fixture.componentRef.setInput('details', true);
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  it('shows the verdict as a heading with a mark, not colour alone', () => {
    const el = render(verdict(['It has 3 of the 20 trial days it needs.']));
    const heading = el.querySelector('h2')!;
    expect(heading.textContent).toContain('Verdict:');
    expect(heading.querySelector('app-status-pill')!.textContent).toContain(
      'Promising, needs more data',
    );
    expect(el.querySelector('app-status-pill')!.getAttribute('data-tone')).toBe('warn');
    expect(el.querySelectorAll('.reasons li').length).toBe(1);
    expect(el.querySelector('details')).toBeNull();
  });

  it('keeps four reasons in view and folds the rest under Details', () => {
    const el = render(verdict(['a.', 'b.', 'c.', 'd.', 'e.', 'f.']), { details: true });
    expect(el.querySelectorAll('section.verdict > ul.reasons li').length).toBe(4);
    const fold = el.querySelector('details')!;
    expect(fold.querySelector('summary')!.textContent).toContain('2 more reasons');
    expect(fold.querySelectorAll('.reasons li').length).toBe(2);
  });

  it('draws the pill alone when compact', () => {
    const el = render(verdict(['x.'], 'worth'), { compact: true });
    expect(el.querySelector('h2')).toBeNull();
    expect(el.textContent?.trim()).toBe('Worth following');
  });
});
