import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { type WritableSignal, signal } from '@angular/core';
import { type ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import { LiveService } from '../../api/live.service';
import type { GateReportView, LiveRulesView, LiveStageView, MarginView } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { SessionService } from '../../core/auth/session.service';
import { StepUpService } from '../../core/auth/step-up.service';
import { ConfirmService } from '../../core/confirm/confirm.service';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import { nextRequest, page, tick } from '../../../testing/http';
import { book } from '../../../testing/portfolio-fixtures';
import { OPTIONS_OFF } from '../../../testing/fake-options-live';
import {
  LiveSettingsPage,
  allocationErrors,
  profileBody,
  profileText,
  switchesToMargin,
} from './live-settings.page';

const LIVE = book({
  id: 'pf_live',
  name: 'Main live',
  kind: 'broker',
  trading: 'live',
  base_currency: 'USD',
});
const PAPER = book({ id: 'pf_paper', name: 'Paper' });

const RULES: LiveRulesView = {
  portfolio_id: 'pf_live',
  safeguards: [
    { name: 'capital_ramp', on: true, settings: { enabled: true } },
    { name: 'price_band', on: false, settings: { band_pct: null } },
    { name: 'account_rules', on: true, settings: { enabled: true } },
  ],
  account_rules_on: true,
  profile_set: false,
  account_rules: [
    { name: 'restricted', applies: false },
    { name: 'settled_cash', applies: false },
  ],
};

const STAGE: LiveStageView = {
  portfolio_id: 'pf_live',
  stage: 'sim_paper',
  next_stage: 'broker_paper',
  real_money: false,
  history: [],
  days: [],
};

const MARGIN_CASH: MarginView = {
  portfolio_id: 'pf_live',
  profile_type: null,
  margin_accounts_on: false,
  account: {
    currency: 'USD',
    equity: 10000,
    cash: 10000,
    available_funds: 9000,
    buying_power: 9000,
    excess_liquidity: null,
    initial_margin: 0,
    maintenance_margin: 0,
    margin_use: 0,
    cushion: null,
    level: null,
    margin_room: null,
    account_type: 'cash',
    reported_type: 'cash',
    day_trades_remaining: null,
  },
  read_error: null,
  buffer: 0.1,
  warn_cushion: 0.15,
  reduce_cushion: 0.1,
  restore_cushion: 0.2,
  pdt: {
    applies: false,
    equity_threshold: 25000,
    max_day_trades: 3,
    window_days: 5,
    day_trades_remaining: null,
  },
  latest_check: null,
};

const REPORT: GateReportView = {
  portfolio_id: 'pf_live',
  from_stage: 'sim_paper',
  target: 'broker_paper',
  passed: false,
  checks: [{ name: 'paper_days', passed: false, detail: 'short of paper days', value: 3 }],
  metrics: { sessions: 0, clean_streak: 0, reject_rate: 0 },
  computed_at: '2026-09-27T20:00:00Z',
};

describe('live settings helpers', () => {
  it('checks the allocation form', () => {
    expect(allocationErrors({ amount: '', currency: 'usd', reason: 'x' }).amount).toBe(
      'Enter an amount.',
    );
    expect(allocationErrors({ amount: '-5', currency: 'USD', reason: 'x' }).amount).toBe(
      'Enter 0 or more.',
    );
    expect(allocationErrors({ amount: '10', currency: 'US', reason: 'x' }).currency).toBeTruthy();
    expect(allocationErrors({ amount: '10', currency: 'USD', reason: ' ' }).reason).toBeTruthy();
    expect(allocationErrors({ amount: '0', currency: 'eur', reason: 'pause' })).toEqual({});
  });

  it('keeps stored profile fields and turns shorts off on a cash account', () => {
    const stored = {
      portfolio_id: 'pf_live',
      jurisdiction: 'us' as const,
      account_type: 'margin' as const,
      client_class: 'retail' as const,
      base_currency: 'USD',
      fx_policy: 'convert' as const,
      wash_sale_mode: 'block' as const,
      allow_short: true,
    };
    const body = profileBody(
      stored,
      { jurisdiction: 'us', account_type: 'cash', client_class: 'retail' },
      'EUR',
    );
    expect(body).toEqual({
      acknowledge_margin_risks: false,
      jurisdiction: 'us',
      account_type: 'cash',
      client_class: 'retail',
      base_currency: 'USD',
      fx_policy: 'convert',
      wash_sale_mode: 'block',
      allow_short: false,
    });
    expect(
      profileBody(null, { jurisdiction: 'uk', account_type: 'cash', client_class: 'retail' }, 'GBP')
        .base_currency,
    ).toBe('GBP');
    expect(profileText({ jurisdiction: 'eu', account_type: 'margin' })).toBe(
      'EU, margin account, retail client',
    );
  });

  it('asks for the margin risks only on a switch to margin', () => {
    expect(switchesToMargin(null, { account_type: 'margin' })).toBe(true);
    expect(switchesToMargin(null, { account_type: 'cash' })).toBe(false);
    const margin = {
      portfolio_id: 'p',
      jurisdiction: 'us' as const,
      account_type: 'margin' as const,
      client_class: 'retail' as const,
      base_currency: 'USD',
      fx_policy: 'refuse' as const,
      wash_sale_mode: 'warn' as const,
      allow_short: false,
    };
    expect(switchesToMargin(margin, { account_type: 'margin' })).toBe(false);
    const body = profileBody(
      null,
      { jurisdiction: 'us', account_type: 'margin', client_class: 'retail' },
      'USD',
      true,
    );
    expect(body.acknowledge_margin_risks).toBe(true);
    expect(
      profileBody(
        null,
        { jurisdiction: 'us', account_type: 'cash', client_class: 'retail' },
        'USD',
        true,
      ).acknowledge_margin_risks,
    ).toBe(false);
  });
});

describe('LiveSettingsPage', () => {
  let fixture: ComponentFixture<LiveSettingsPage>;
  let http: HttpTestingController;
  let allowed: boolean;
  let confirm: ReturnType<typeof vi.spyOn>;
  let ensure: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    allowed = true;
    TestBed.configureTestingModule({
      providers: [provideRouter([]), ...provideApi(), provideHttpClientTesting()],
    });
    http = TestBed.inject(HttpTestingController);
    const session = TestBed.inject(SessionService);
    vi.spyOn(session, 'can').mockImplementation(() => allowed);
    vi.spyOn(session, 'whyNot').mockImplementation(() => (allowed ? null : 'Traders only.'));
    confirm = vi.spyOn(TestBed.inject(ConfirmService), 'confirm').mockResolvedValue(true);
    ensure = vi.spyOn(TestBed.inject(StepUpService), 'ensure').mockResolvedValue(true);
  });

  afterEach(() => http.verify());

  async function render(
    id = 'pf_live',
    profile: unknown = null,
    stage: LiveStageView = STAGE,
  ): Promise<HTMLElement> {
    fixture = TestBed.createComponent(LiveSettingsPage);
    fixture.componentRef.setInput('id', id);
    fixture.detectChanges();
    (await nextRequest(http, '/api/portfolios')).flush(page([LIVE, PAPER]));
    await tick();
    fixture.detectChanges();
    if (id === 'pf_live') {
      (await nextRequest(http, '/api/portfolios/pf_live/live/allocation')).flush({
        portfolio_id: 'pf_live',
        amount: null,
        currency: null,
        reason: null,
        updated_at: null,
        updated_by: null,
      });
      const prof = await nextRequest(http, '/api/portfolios/pf_live/live/account-profile');
      if (profile) prof.flush(profile);
      else prof.flush({ detail: 'none' }, { status: 404, statusText: 'Not Found' });
      (await nextRequest(http, '/api/portfolios/pf_live/live/rules')).flush(RULES);
      (await nextRequest(http, '/api/portfolios/pf_live/live/stage')).flush(stage);
      (await nextRequest(http, '/api/portfolios/pf_live/live/gate-report')).flush(REPORT);
      (await nextRequest(http, '/api/portfolios/pf_live/live/margin')).flush(MARGIN_CASH);
      await tick();
      fixture.detectChanges();
      (await nextRequest(http, '/api/portfolios/pf_live/live/options')).flush(OPTIONS_OFF);
      await tick();
      fixture.detectChanges();
    }
    return fixture.nativeElement as HTMLElement;
  }

  function type(el: HTMLElement, selector: string, value: string) {
    const input = el.querySelector<HTMLInputElement | HTMLTextAreaElement>(selector)!;
    input.value = value;
    input.dispatchEvent(new Event('input'));
  }

  function allocationForm(el: HTMLElement): HTMLFormElement {
    return el.querySelector<HTMLFormElement>('form[aria-labelledby="allocation-form-title"]')!;
  }

  function button(el: HTMLElement, text: string) {
    return [...el.querySelectorAll('button')].find((b) => b.textContent?.trim() === text)!;
  }

  it('shows an unset allocation, and says there are no automatic steps', async () => {
    const el = await render();
    expect(el.querySelector('[data-testid="allocation-figure"]')?.textContent).toContain('Not set');
    expect(el.textContent).toContain('Nothing opens in this account until you set an amount.');
    expect(el.querySelector('.no-ramp')?.textContent).toContain('No automatic steps');
    expect(el.querySelector('h1')?.textContent).toContain('Real-money settings');
  });

  it('shows no brass and a PAPER stamp while no real money moves', async () => {
    const el = await render();
    expect(el.querySelector('.live-frame')).toBeNull();
    expect(el.querySelector('app-page-header app-mode-stamp')?.textContent).toContain('PAPER');
    expect(button(el, 'Set allocation').classList).not.toContain('btn-danger');
  });

  it('keeps brass and the LIVE stamp for a Real money stage', async () => {
    const el = await render('pf_live', null, {
      ...STAGE,
      stage: 'live_small',
      next_stage: 'live_scale',
      real_money: true,
    });
    expect(el.querySelector('.live-frame')).not.toBeNull();
    expect(el.querySelector('app-page-header app-mode-stamp')?.textContent).toContain('LIVE');
    expect(button(el, 'Set allocation').classList).toContain('btn-danger');
  });

  it('links to the Going live checklist for this portfolio', async () => {
    const el = await render();
    const link = [...el.querySelectorAll<HTMLAnchorElement>('a')].find((a) =>
      a.textContent?.includes('Going live checklist'),
    );
    expect(link?.getAttribute('href')).toBe('/going-live?portfolio=pf_live');
  });

  it('sets the allocation after the ticket and a fresh code', async () => {
    const el = await render();
    type(el, '#allocation-amount', '2500');
    type(el, '#allocation-reason', 'first slice');
    allocationForm(el).dispatchEvent(new Event('submit'));
    const put = await nextRequest(http, '/api/portfolios/pf_live/live/allocation', 'PUT');
    expect(confirm).toHaveBeenCalledTimes(1);
    const ticket = confirm.mock.calls[0][0] as { ticket: { live: boolean; kind: string } };
    // At Simulated no real money moves yet: a PAPER ticket.
    expect(ticket.ticket).toMatchObject({ live: false, kind: 'Allocation' });
    expect(ensure).toHaveBeenCalledTimes(1);
    expect(put.request.body).toEqual({ amount: 2500, currency: 'USD', reason: 'first slice' });
    put.flush({
      portfolio_id: 'pf_live',
      amount: 2500,
      currency: 'USD',
      reason: 'first slice',
      updated_at: '2026-09-27T12:00:00Z',
      updated_by: 'user:u1',
    });
    (await nextRequest(http, '/api/portfolios/pf_live/live/rules')).flush(RULES);
    await tick();
    fixture.detectChanges();
    expect(el.querySelector('[data-testid="allocation-figure"]')?.textContent).toContain('2,500');
  });

  it('names the allocation reason apart from the options level reason', async () => {
    const el = await render();
    expect(el.querySelector('label[for="allocation-reason"]')?.textContent?.trim()).toBe(
      'Reason for the allocation',
    );
  });

  it('needs a reason before anything is sent', async () => {
    const el = await render();
    type(el, '#allocation-amount', '100');
    allocationForm(el).dispatchEvent(new Event('submit'));
    await tick();
    fixture.detectChanges();
    expect(el.textContent).toContain('Say why.');
    expect(confirm).not.toHaveBeenCalled();
  });

  it('sends nothing when the code is not given', async () => {
    ensure.mockResolvedValue(false);
    const el = await render();
    type(el, '#allocation-amount', '100');
    type(el, '#allocation-reason', 'try');
    allocationForm(el).dispatchEvent(new Event('submit'));
    await tick();
    http.expectNone({ method: 'PUT' });
  });

  it('saves an account profile and explains what it means', async () => {
    const el = await render();
    expect(el.textContent).toContain('Not set. With the account rules on, nothing opens');
    const uk = [...el.querySelectorAll<HTMLButtonElement>('[role="radio"]')].find(
      (b) => b.textContent?.trim() === 'UK',
    )!;
    uk.click();
    fixture.detectChanges();
    expect(el.querySelector('.notes')?.textContent).toContain('two trading days later');
    button(el, 'Save profile').click();
    const put = await nextRequest(http, '/api/portfolios/pf_live/live/account-profile', 'PUT');
    expect(put.request.body).toMatchObject({
      jurisdiction: 'uk',
      account_type: 'cash',
      client_class: 'retail',
      allow_short: false,
    });
    put.flush({ portfolio_id: 'pf_live', ...put.request.body });
    (await nextRequest(http, '/api/portfolios/pf_live/live/rules')).flush({
      ...RULES,
      profile_set: true,
      account_rules: [
        { name: 'restricted', applies: true },
        { name: 'settled_cash', applies: true },
      ],
    });
    await tick();
    fixture.detectChanges();
    expect(el.textContent).toContain('UK, cash account, retail client');
    expect(el.textContent).toContain('Settled cash only');
  });

  it('shows the risks of margin and needs them ticked before a switch', async () => {
    const el = await render();
    expect(el.querySelector('.margin-risks')).toBeNull();
    const margin = [...el.querySelectorAll<HTMLButtonElement>('[role="radio"]')].find(
      (b) => b.textContent?.trim() === 'Margin',
    )!;
    margin.click();
    fixture.detectChanges();
    const risks = el.querySelector('.margin-risks');
    expect(risks?.textContent).toContain('You can lose more than you put in.');
    expect(risks?.textContent).toContain('sell your positions without asking');
    expect(button(el, 'Save profile').disabled).toBe(true);
    const ack = el.querySelector<HTMLInputElement>('[data-testid="margin-ack"]')!;
    ack.checked = true;
    ack.dispatchEvent(new Event('change'));
    fixture.detectChanges();
    expect(button(el, 'Save profile').disabled).toBe(false);
    button(el, 'Save profile').click();
    const put = await nextRequest(http, '/api/portfolios/pf_live/live/account-profile', 'PUT');
    expect(put.request.body).toMatchObject({
      account_type: 'margin',
      acknowledge_margin_risks: true,
    });
    put.flush({ detail: 'margin accounts are off' }, { status: 409, statusText: 'Conflict' });
    await tick();
  });

  it('shows buying power from the broker', async () => {
    const el = await render();
    const panel = el.querySelector('app-live-margin-panel');
    expect(panel?.querySelector('[data-testid="buying-power"]')?.textContent).toContain('9,000');
    expect(panel?.textContent).toContain('A cash account borrows nothing');
  });

  it('lists which safeguards are on, read only', async () => {
    const el = await render();
    const items = [...el.querySelectorAll('.rules li')].map((li) => li.textContent ?? '');
    expect(items.find((t) => t.includes('Allocation cap'))).toContain('On');
    expect(items.find((t) => t.includes('Price band'))).toContain('Off');
    expect(el.textContent).toContain('no profile is saved, so nothing opens');
  });

  it('turns controls off for someone who may not change live settings', async () => {
    allowed = false;
    const el = await render();
    expect(button(el, 'Set allocation').disabled).toBe(true);
    expect(button(el, 'Save profile').disabled).toBe(true);
  });

  it('shows the stage card and the preview panel on a live portfolio', async () => {
    const el = await render();
    expect(el.querySelector('app-live-stage-card')?.textContent).toContain('Simulated');
    expect(el.querySelector('app-live-preview-panel')?.textContent).toContain('Nothing is sent');
  });

  it('explains a paper portfolio has no live settings', async () => {
    const el = await render('pf_paper');
    expect(el.textContent).toContain('This portfolio trades on paper');
  });
});

describe('LiveSettingsPage stage per portfolio', () => {
  it('forgets the last portfolio stage when the page moves to another one', async () => {
    const never = () => new Promise<never>(() => undefined);
    TestBed.overrideComponent(LiveSettingsPage, { set: { template: '', imports: [] } });
    TestBed.configureTestingModule({
      providers: [
        provideRouter([]),
        {
          provide: LiveService,
          useValue: { allocation: never, profile: never, rules: never },
        },
        {
          provide: PortfolioContextService,
          useValue: {
            options: signal([LIVE, { ...LIVE, id: 'pf_other', name: 'Other' }]),
            load: () => Promise.resolve(),
          },
        },
      ],
    });
    const fixture = TestBed.createComponent(LiveSettingsPage);
    const page = fixture.componentInstance as unknown as {
      stage: WritableSignal<string | null>;
      realMoney: () => boolean;
    };
    fixture.componentRef.setInput('id', 'pf_live');
    fixture.detectChanges();
    page.stage.set('live_small');
    expect(page.realMoney()).toBe(true);
    // The router reuses the page for /profile/live/pf_other: no brass before its stage loads.
    fixture.componentRef.setInput('id', 'pf_other');
    fixture.detectChanges();
    expect(page.realMoney()).toBe(false);
  });
});
