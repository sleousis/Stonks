import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';

import type { PortfolioRef } from '../../api/portfolios.service';
import { provideApi } from '../../api/provide-api';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import { nextRequest, page, tick } from '../../../testing/http';
import { book } from '../../../testing/portfolio-fixtures';
import { PortfolioPicker } from './portfolio-picker';

describe('PortfolioPicker', () => {
  let http: HttpTestingController;

  beforeEach(() => {
    localStorage.removeItem('stonks.portfolio');
    TestBed.configureTestingModule({ providers: [...provideApi(), provideHttpClientTesting()] });
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    http.verify();
    localStorage.removeItem('stonks.portfolio');
  });

  async function render(books: PortfolioRef[]) {
    const fixture = TestBed.createComponent(PortfolioPicker);
    fixture.detectChanges();
    (await nextRequest(http, '/api/portfolios')).flush(page(books));
    await tick(2);
    fixture.detectChanges();
    return { fixture, el: fixture.nativeElement as HTMLElement };
  }

  const options = (el: HTMLElement) =>
    [...el.querySelectorAll('option')].map((o) => ({
      text: o.textContent?.trim(),
      selected: o.selected,
    }));

  it('hides itself with a single portfolio', async () => {
    const { el } = await render([book({ id: 'pf_1', name: 'Main', is_default: true })]);
    expect(el.querySelector('select')).toBeNull();
  });

  it('lists each portfolio once, the default shown as picked (UX-69)', async () => {
    const { el } = await render([
      book({ id: 'pf_1', name: 'Main', is_default: true }),
      book({ id: 'pf_2', name: 'Real money', trading: 'live' }),
    ]);
    expect(options(el)).toEqual([
      { text: 'Main', selected: true },
      { text: 'Real money (live)', selected: false },
    ]);
    expect(el.textContent).not.toContain('My default portfolio');
    expect(el.querySelector('app-mode-stamp')?.textContent).toContain('PAPER');
  });

  it('switches the money pages to the picked portfolio and stamps it LIVE', async () => {
    const { fixture, el } = await render([
      book({ id: 'pf_1', name: 'Main', is_default: true }),
      book({ id: 'pf_2', name: 'Real money', trading: 'live' }),
    ]);
    const select = el.querySelector('select')!;
    select.value = 'pf_2';
    select.dispatchEvent(new Event('change'));
    fixture.detectChanges();
    const ctx = TestBed.inject(PortfolioContextService);
    expect(ctx.selectedId()).toBe('pf_2');
    expect(ctx.query()).toEqual({ portfolio_id: 'pf_2' });
    expect(el.querySelector('app-mode-stamp')?.textContent).toContain('LIVE');
  });

  it('reads a broker portfolio at Broker paper as paper, never LIVE or brass', async () => {
    const { fixture, el } = await render([
      book({ id: 'pf_1', name: 'Main', is_default: true }),
      book({ id: 'pf_2', name: 'IBKR paper', kind: 'broker', live_stage: 'broker_paper' }),
    ]);
    expect(options(el)[1]).toEqual({ text: 'IBKR paper (broker paper)', selected: false });
    const select = el.querySelector('select')!;
    select.value = 'pf_2';
    select.dispatchEvent(new Event('change'));
    fixture.detectChanges();
    const stamp = el.querySelector('app-mode-stamp .stamp')!;
    expect(stamp.textContent).toContain('BROKER PAPER');
    expect(stamp.textContent).not.toContain('LIVE');
    expect(stamp.getAttribute('data-mode')).toBe('paper');
  });

  it('asks for a pick when no portfolio is the default', async () => {
    const { el } = await render([book({ id: 'pf_1', name: 'A' }), book({ id: 'pf_2', name: 'B' })]);
    expect(options(el)[0]).toEqual({ text: 'Pick a portfolio', selected: true });
    expect(options(el)).toHaveLength(3);
  });
});
