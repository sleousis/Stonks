import { TestBed } from '@angular/core/testing';
import { ActivatedRoute, provideRouter } from '@angular/router';

import { GLOSSARY, GLOSSARY_KEYS, METRIC_KEYS } from '../../core/help/glossary';
import { GlossaryPage } from './glossary.page';

describe('GlossaryPage', () => {
  function render(fragment: string | null = null) {
    TestBed.configureTestingModule({
      providers: [
        provideRouter([]),
        { provide: ActivatedRoute, useValue: { snapshot: { fragment } } },
      ],
    });
    const fixture = TestBed.createComponent(GlossaryPage);
    fixture.detectChanges();
    return { fixture, el: fixture.nativeElement as HTMLElement };
  }

  it('lists every glossary term with an anchor named after its key', () => {
    const { el } = render();
    expect(el.querySelectorAll('.term').length).toBe(GLOSSARY_KEYS.length);
    const sharpe = el.querySelector('#sharpe')!;
    expect(sharpe.textContent).toContain(GLOSSARY.sharpe.term);
    expect(sharpe.textContent).toContain(GLOSSARY.sharpe.short);
  });

  it('opens with the trading words, then the figures (UX-42)', () => {
    const { el } = render();
    const headings = [...el.querySelectorAll('section.group h2')].map((h) => h.textContent);
    expect(headings).toEqual(['Trading words', 'Your money and alerts', 'Figures']);
    const trading = el.querySelector('section.group')!;
    expect(trading.querySelector('#kill_switch')!.textContent).toContain('Kill switch');
    expect(trading.querySelector('#dry_run')).not.toBeNull();
  });

  it('filters by a word in the term or its explanation', () => {
    const { el, fixture } = render();
    const input = el.querySelector<HTMLInputElement>('#glossary-filter')!;
    input.value = 'drawdown';
    input.dispatchEvent(new Event('input'));
    fixture.detectChanges();
    const terms = [...el.querySelectorAll('.term dt')].map((d) => d.textContent);
    expect(terms.length).toBeGreaterThan(0);
    expect(terms.length).toBeLessThan(METRIC_KEYS.length);
    expect(terms).toContain(GLOSSARY.max_drawdown.term);
  });

  it('says so when nothing matches', () => {
    const { el, fixture } = render();
    const input = el.querySelector<HTMLInputElement>('#glossary-filter')!;
    input.value = 'zzzz';
    input.dispatchEvent(new Event('input'));
    fixture.detectChanges();
    expect(el.textContent).toContain('No term matches');
  });

  it('marks the linked term', () => {
    const { el } = render('max_drawdown');
    expect(el.querySelector('#max_drawdown')!.classList).toContain('target');
  });
});
