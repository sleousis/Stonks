import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { type ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import type { LiveRulesView } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { SessionService } from '../../core/auth/session.service';
import { StepUpService } from '../../core/auth/step-up.service';
import { ConfirmService } from '../../core/confirm/confirm.service';
import { nextRequest, page, tick } from '../../../testing/http';
import { book } from '../../../testing/portfolio-fixtures';
import { LiveSettingsPage, allocationErrors, profileBody, profileText } from './live-settings.page';

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

  async function render(id = 'pf_live', profile: unknown = null): Promise<HTMLElement> {
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

  function button(el: HTMLElement, text: string) {
    return [...el.querySelectorAll('button')].find((b) => b.textContent?.trim() === text)!;
  }

  it('shows an unset allocation, and says there are no automatic steps', async () => {
    const el = await render();
    expect(el.querySelector('[data-testid="allocation-figure"]')?.textContent).toContain('Not set');
    expect(el.textContent).toContain('Nothing opens in this account until you set an amount.');
    expect(el.querySelector('.no-ramp')?.textContent).toContain('No automatic steps');
    expect(el.querySelector('.live-frame')).not.toBeNull();
    expect(el.textContent).toContain('LIVE');
  });

  it('sets the allocation after the ticket and a fresh code', async () => {
    const el = await render();
    type(el, '#allocation-amount', '2500');
    type(el, '#allocation-reason', 'first slice');
    el.querySelector<HTMLFormElement>('form')!.dispatchEvent(new Event('submit'));
    const put = await nextRequest(http, '/api/portfolios/pf_live/live/allocation', 'PUT');
    expect(confirm).toHaveBeenCalledTimes(1);
    const ticket = confirm.mock.calls[0][0] as { ticket: { live: boolean; kind: string } };
    expect(ticket.ticket).toMatchObject({ live: true, kind: 'Allocation' });
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

  it('needs a reason before anything is sent', async () => {
    const el = await render();
    type(el, '#allocation-amount', '100');
    el.querySelector<HTMLFormElement>('form')!.dispatchEvent(new Event('submit'));
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
    el.querySelector<HTMLFormElement>('form')!.dispatchEvent(new Event('submit'));
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

  it('lists which live safeguards are on, read only', async () => {
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

  it('explains a paper portfolio has no live settings', async () => {
    const el = await render('pf_paper');
    expect(el.textContent).toContain('This portfolio trades on paper');
  });
});
