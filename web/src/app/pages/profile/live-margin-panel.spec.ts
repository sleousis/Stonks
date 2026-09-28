import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { type ComponentFixture, TestBed } from '@angular/core/testing';

import type { MarginView } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { marginLevelWords } from '../../shared/live-rules';
import { nextRequest, tick } from '../../../testing/http';
import { LiveMarginPanel, marginMeter, pdtText } from './live-margin-panel';

const MARGIN: MarginView = {
  portfolio_id: 'pf_live',
  profile_type: 'margin',
  margin_accounts_on: true,
  account: {
    currency: 'USD',
    equity: 20000,
    cash: 1000,
    available_funds: 5000,
    buying_power: 20000,
    excess_liquidity: 6000,
    initial_margin: 12000,
    maintenance_margin: 14000,
    margin_use: 0.7,
    cushion: 0.3,
    level: 'ok',
    margin_room: 6000,
    account_type: 'margin',
    reported_type: 'margin',
    day_trades_remaining: 2,
  },
  read_error: null,
  buffer: 0.1,
  warn_cushion: 0.15,
  reduce_cushion: 0.1,
  restore_cushion: 0.2,
  pdt: {
    applies: true,
    equity_threshold: 25000,
    max_day_trades: 3,
    window_days: 5,
    day_trades_remaining: 2,
  },
  latest_check: {
    checked_at: '2026-09-28T15:00:00Z',
    source: 'monitor',
    level: 'ok',
    cushion: 0.3,
    equity: 20000,
    maintenance_margin: 14000,
  },
};

describe('margin words', () => {
  it('fills the meter with margin use, within 0 and 1', () => {
    expect(marginMeter(MARGIN)).toBe(0.7);
    expect(marginMeter({ account: null })).toBe(0);
    expect(marginMeter({ account: { ...MARGIN.account!, margin_use: 1.4 } })).toBe(1);
  });

  it('says the pattern day trader state only when it binds', () => {
    expect(pdtText(MARGIN)).toContain('at most 3 day trades in 5 trading days. 2 left now.');
    expect(pdtText({ pdt: { ...MARGIN.pdt, applies: false } })).toBeNull();
    expect(pdtText({ pdt: { ...MARGIN.pdt, day_trades_remaining: null } })).toContain('no count');
  });

  it('names each level plainly', () => {
    expect(marginLevelWords('ok').label).toBe('Healthy');
    expect(marginLevelWords('reduce').effect).toContain('before the broker does');
    expect(marginLevelWords('call').tone).toBe('negative');
    expect(marginLevelWords(null).label).toBe('No margin');
  });
});

describe('LiveMarginPanel', () => {
  let fixture: ComponentFixture<LiveMarginPanel>;
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({ providers: [...provideApi(), provideHttpClientTesting()] });
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  async function render(view: MarginView): Promise<HTMLElement> {
    fixture = TestBed.createComponent(LiveMarginPanel);
    fixture.componentRef.setInput('portfolioId', 'pf_live');
    fixture.detectChanges();
    (await nextRequest(http, '/api/portfolios/pf_live/live/margin')).flush(view);
    await tick();
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  it('shows buying power, margin use and the cushion of a margin account', async () => {
    const el = await render(MARGIN);
    expect(el.querySelector('[data-testid="buying-power"]')?.textContent).toContain('20,000');
    expect(el.querySelector('[data-testid="margin-room"]')?.textContent).toContain('6,000');
    expect(el.querySelector('[data-testid="margin-use"]')?.textContent).toContain('70');
    expect(el.querySelector('[data-testid="cushion"]')?.textContent).toContain('30');
    const meter = el.querySelector('[role="meter"]')!;
    expect(meter.getAttribute('aria-valuenow')).toBe('70');
    expect(el.textContent).toContain('Healthy');
    expect(el.querySelector('[data-testid="pdt"]')?.textContent).toContain('2 left now');
    expect(el.textContent).toContain('during the session');
  });

  it('warns plainly when the account needs reducing', async () => {
    const el = await render({
      ...MARGIN,
      account: { ...MARGIN.account!, level: 'reduce', cushion: 0.05 },
    });
    expect(el.textContent).toContain('Reducing');
    expect(el.querySelector('.level-effect')?.textContent).toContain('before the broker does');
    expect(el.querySelector('.meter')?.getAttribute('data-level')).toBe('reduce');
  });

  it('says when the broker does not report a margin account', async () => {
    const el = await render({
      ...MARGIN,
      account: { ...MARGIN.account!, reported_type: 'cash' },
    });
    expect(el.textContent).toContain('does not report this as a margin account');
  });

  it('says why the account could not be read', async () => {
    const el = await render({ ...MARGIN, account: null, read_error: 'gateway down' });
    expect(el.querySelector('[data-testid="margin-read-error"]')?.textContent).toContain(
      'gateway down',
    );
  });

  it('reads again on demand', async () => {
    const el = await render(MARGIN);
    const again = [...el.querySelectorAll('button')].find(
      (b) => b.textContent?.trim() === 'Read again',
    )!;
    again.click();
    (await nextRequest(http, '/api/portfolios/pf_live/live/margin')).flush(MARGIN);
    await tick();
  });
});
