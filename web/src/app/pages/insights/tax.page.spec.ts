import {
  HttpTestingController,
  type TestRequest,
  provideHttpClientTesting,
} from '@angular/common/http/testing';
import { type ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import type { TaxSettingsView, TaxYearView } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { SessionService } from '../../core/auth/session.service';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import { nextRequest, page, tick } from '../../../testing/http';
import { TaxPage, localDay, taxYears } from './tax.page';

const query = (req: TestRequest) => new URL(req.request.urlWithParams, 'http://x').searchParams;

const SETTINGS: TaxSettingsView = {
  portfolio_id: 'pf_1',
  base_currency: 'USD',
  jurisdiction: 'us',
  lot_method: 'fifo',
  wash_sales: true,
  updated_at: null,
};

const YEAR: TaxYearView = {
  portfolio_id: 'pf_1',
  year: 2026,
  base_currency: 'USD',
  jurisdiction: 'us',
  short_term_gain: 200,
  long_term_gain: 500,
  wash_sale_disallowed: 0,
  estimated_tax: 139,
  disposals: 3,
  unconverted: 0,
  rates: { short_term: 0.32, long_term: 0.15 },
};

describe('TaxPage', () => {
  let fixture: ComponentFixture<TaxPage>;
  let http: HttpTestingController;
  let el: HTMLElement;
  let allowed: boolean;
  let settings: TaxSettingsView;

  beforeEach(() => {
    allowed = true;
    settings = SETTINGS;
    TestBed.configureTestingModule({
      providers: [
        ...provideApi(),
        provideHttpClientTesting(),
        provideRouter([]),
        {
          provide: PortfolioContextService,
          useValue: {
            selectedId: () => 'pf_1',
            current: () => null,
            live: () => false,
            state: () => 'ready',
            noBook: () => false,
            query: () => ({ portfolio_id: 'pf_1' }),
          },
        },
        {
          provide: SessionService,
          useValue: { can: () => allowed, whyNot: () => null, csrfToken: () => null },
        },
      ],
    });
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  function respond(req: TestRequest): void {
    const path = new URL(req.request.urlWithParams, 'http://x').pathname;
    if (path === '/api/tax/settings') return req.flush(settings);
    if (path === '/api/orders/fills') return req.flush(page([]));
    if (path === '/api/tax/year') return req.flush(YEAR);
    throw new Error(`unexpected ${path}`);
  }

  async function flushAll(): Promise<void> {
    for (let i = 0; i < 5; i++) {
      http.match((r) => r.method === 'GET').forEach(respond);
      await tick(5);
      fixture.detectChanges();
    }
  }

  async function render(): Promise<void> {
    fixture = TestBed.createComponent(TaxPage);
    el = fixture.nativeElement;
    fixture.detectChanges();
    await flushAll();
  }

  function radio(text: string): HTMLButtonElement {
    return [...el.querySelectorAll<HTMLButtonElement>('[role=radio]')].find(
      (b) => b.textContent?.trim() === text,
    )!;
  }

  function saveButton(): HTMLButtonElement {
    return [...el.querySelectorAll('button')].find((b) =>
      b.textContent?.includes('Save settings'),
    )!;
  }

  it('shows the settings and saves only what changed', async () => {
    await render();
    expect(el.querySelector<HTMLInputElement>('#tax-base')!.value).toBe('USD');
    expect(radio('United States').getAttribute('aria-checked')).toBe('true');
    expect(el.textContent).toContain('Wash sale adjustment');
    expect(saveButton().disabled).toBe(true);

    radio('Specific lots').click();
    fixture.detectChanges();
    expect(saveButton().disabled).toBe(false);
    el.querySelector<HTMLFormElement>('form')!.dispatchEvent(new Event('submit'));
    const req = await nextRequest(http, '/api/tax/settings', 'PUT');
    expect(req.request.body).toEqual({
      base_currency: 'USD',
      jurisdiction: 'us',
      lot_method: 'specific',
      wash_sales: true,
    });
    settings = { ...SETTINGS, lot_method: 'specific' };
    req.flush(settings);
    await flushAll();
    expect(el.textContent).toContain('No sales yet');
  });

  it('says when and why base currency and filing place are locked (real money)', async () => {
    settings = { ...SETTINGS, locked: true };
    await render();
    const note = el.querySelector('.locked')!;
    expect(note.textContent).toContain('locked while this portfolio trades real money');
    expect(note.textContent).toContain('Broker paper');
    expect(note.querySelector('a[href="/profile/live/pf_1"]')).not.toBeNull();
    expect(el.querySelector<HTMLInputElement>('#tax-base')!.disabled).toBe(true);
    // Where you file shows as text, not as choices; the lot method can still change.
    expect(radio('European Union')).toBeUndefined();
    expect(el.textContent).toContain('United States');
    expect(radio('Specific lots')).toBeDefined();
  });

  it('shows no lock note on a paper portfolio', async () => {
    await render();
    expect(el.querySelector('.locked')).toBeNull();
    expect(el.querySelector<HTMLInputElement>('#tax-base')!.disabled).toBe(false);
  });

  it('hides wash sales outside the US and checks the currency code', async () => {
    await render();
    radio('European Union').click();
    fixture.detectChanges();
    expect(el.textContent).not.toContain('Wash sale adjustment');
    const base = el.querySelector<HTMLInputElement>('#tax-base')!;
    base.value = 'eu';
    base.dispatchEvent(new Event('input'));
    fixture.detectChanges();
    el.querySelector<HTMLFormElement>('form')!.dispatchEvent(new Event('submit'));
    fixture.detectChanges();
    expect(el.textContent).toContain('Enter a three-letter currency code');
    http.expectNone({ method: 'PUT' });
  });

  it('downloads the gains file of the picked year', async () => {
    await render();
    const create = vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:x');
    const revoke = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined);
    const clicks: string[] = [];
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (
      this: HTMLAnchorElement,
    ) {
      clicks.push(this.download);
    });
    try {
      const year = el.querySelector<HTMLSelectElement>('#tax-year')!;
      year.value = String(taxYears()[1]);
      year.dispatchEvent(new Event('change'));
      fixture.detectChanges();
      [...el.querySelectorAll('button')]
        .find((b) => b.textContent?.includes('Realized gains CSV'))!
        .click();
      const req = await nextRequest(http, '/api/tax/exports/gains');
      expect(query(req).get('year')).toBe(String(taxYears()[1]));
      expect(query(req).get('portfolio_id')).toBe('pf_1');
      req.flush(new Blob(['ticker\n'], { type: 'text/csv' }));
      await tick(5);
      expect(clicks).toEqual([`stonks-tax-gains-${taxYears()[1]}.csv`]);
    } finally {
      create.mockRestore();
      revoke.mockRestore();
      click.mockRestore();
    }
  });

  it('downloads the open lots on the picked day (13.12)', async () => {
    await render();
    const create = vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:x');
    const revoke = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined);
    const clicks: string[] = [];
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (
      this: HTMLAnchorElement,
    ) {
      clicks.push(this.download);
    });
    try {
      const day = el.querySelector<HTMLInputElement>('#tax-lots-day')!;
      expect(day.value).toBe(localDay(new Date()));
      expect(el.querySelector('label[for="tax-lots-day"]')?.textContent).toContain('Open lots on');
      day.value = '2026-06-30';
      day.dispatchEvent(new Event('change'));
      fixture.detectChanges();
      [...el.querySelectorAll('button')]
        .find((b) => b.textContent?.includes('Open lots CSV'))!
        .click();
      const req = await nextRequest(http, '/api/tax/exports/lots');
      expect(query(req).get('as_of')).toBe('2026-06-30');
      expect(query(req).get('portfolio_id')).toBe('pf_1');
      req.flush(new Blob(['ticker\n'], { type: 'text/csv' }));
      await tick(5);
      expect(clicks).toEqual(['stonks-tax-lots-2026-06-30.csv']);
    } finally {
      create.mockRestore();
      revoke.mockRestore();
      click.mockRestore();
    }
  });

  it('writes a local day as YYYY-MM-DD', () => {
    expect(localDay(new Date(2026, 0, 5))).toBe('2026-01-05');
  });

  it('disables changes for people who may not manage the portfolio', async () => {
    allowed = false;
    await render();
    expect(el.querySelector<HTMLInputElement>('#tax-base')!.disabled).toBe(true);
    expect(saveButton().disabled).toBe(true);
  });

  it('offers this year and the five before it', () => {
    expect(taxYears(new Date('2026-03-01T00:00:00Z'))).toEqual([
      2026, 2025, 2024, 2023, 2022, 2021,
    ]);
  });
  it('shows the tax owed this year', async () => {
    await render();
    const card = el.querySelector('app-tax-year-card')!;
    expect(card.textContent).toContain('Tax owed this year');
    expect(card.textContent).toContain('$139.00');
    expect(card.textContent).toContain('3 sales in 2026');
  });
});
