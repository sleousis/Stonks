import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { type ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import { provideApi } from '../../api/provide-api';
import { SessionService } from '../../core/auth/session.service';
import { TRADER } from '../../../testing/auth-fixtures';
import { nextRequest, page, tick } from '../../../testing/http';
import { book } from '../../../testing/portfolio-fixtures';
import { GoingLivePage } from './going-live.page';
import { rememberPreview } from './preview-memory';

const BROKER = book({
  id: 'pf_b',
  name: 'Sam at IBKR',
  kind: 'broker',
  trading: 'live',
  is_default: true,
});
const PAPER = book({ id: 'pf_p', name: 'Paper' });

describe('GoingLivePage', () => {
  let fixture: ComponentFixture<GoingLivePage>;
  let http: HttpTestingController;

  beforeEach(async () => {
    TestBed.configureTestingModule({
      providers: [provideRouter([]), ...provideApi(), provideHttpClientTesting()],
    });
    http = TestBed.inject(HttpTestingController);
    const session = TestBed.inject(SessionService);
    const loading = session.load();
    (await nextRequest(http, '/api/auth/me')).flush({ ...TRADER, via: 'session' });
    await loading;
  });

  afterEach(() => {
    http.verify();
    localStorage.clear();
  });

  async function settle() {
    for (let i = 0; i < 3; i++) {
      await tick();
      fixture.detectChanges();
    }
  }

  async function render(opts: { portfolio?: string; realMoney?: boolean } = {}) {
    fixture = TestBed.createComponent(GoingLivePage);
    if (opts.portfolio) fixture.componentRef.setInput('portfolio', opts.portfolio);
    fixture.detectChanges();
    (await nextRequest(http, '/api/portfolios')).flush(page([BROKER, PAPER]));
    (await nextRequest(http, '/api/brokers/gateways')).flush({ configured: false, gateways: [] });
    (await nextRequest(http, '/api/connections/providers')).flush([]);
    (await nextRequest(http, '/api/connections')).flush(page([]));
    (await nextRequest(http, '/api/subscriptions')).flush(page([]));
    await settle();
    return fixture.nativeElement as HTMLElement;
  }

  async function answerBroker(realMoney = false) {
    (await nextRequest(http, '/api/portfolios/pf_b/live/stage')).flush({
      portfolio_id: 'pf_b',
      stage: realMoney ? 'live_small' : 'broker_paper',
      next_stage: realMoney ? 'live_scale' : 'live_small',
      real_money: realMoney,
      history: [],
      days: [],
    });
    (await nextRequest(http, '/api/portfolios/pf_b/live/gate-report')).flush({
      portfolio_id: 'pf_b',
      from_stage: 'broker_paper',
      target: 'live_small',
      passed: false,
      checks: [],
      metrics: {},
      computed_at: '2026-09-27T20:00:00Z',
    });
    (await nextRequest(http, '/api/portfolios/pf_b/live/allocation')).flush({
      portfolio_id: 'pf_b',
      amount: 1000,
      currency: 'USD',
      reason: 'start',
      updated_at: '2026-09-27T10:00:00Z',
      updated_by: 'user:u',
    });
    (await nextRequest(http, '/api/portfolios/pf_b/live/account-profile')).flush(
      { detail: 'none' },
      { status: 404, statusText: 'Not Found' },
    );
    (await nextRequest(http, '/api/portfolios/pf_b/live/rules')).flush({
      portfolio_id: 'pf_b',
      safeguards: [{ name: 'capital_ramp', on: true, settings: {} }],
      account_rules_on: false,
      profile_set: false,
      account_rules: [],
    });
    await settle();
  }

  it('lists the eight steps in order with done or not and a link', async () => {
    rememberPreview('pf_b', '2026-09-27T10:00:00Z');
    const el = await render();
    await answerBroker();
    const steps = [...el.querySelectorAll('li.step')];
    expect(steps.map((s) => s.getAttribute('data-step'))).toEqual([
      'gateway',
      'broker',
      'stage',
      'allocation',
      'profile',
      'safeguards',
      'preview',
      'mode',
    ]);
    const state = (key: string) =>
      el.querySelector(`li[data-step="${key}"] .state`)!.textContent!.trim();
    expect(state('gateway')).toBe('Waiting for your admin');
    expect(state('allocation')).toBe('Done');
    expect(state('profile')).toBe('Not yet');
    expect(state('preview')).toBe('Done');
    expect(el.querySelector('.progress')!.textContent).toContain('of 8');
    expect(el.querySelector('.progress')!.textContent).toContain('Broker paper');
    const profileLink = el.querySelector<HTMLAnchorElement>('li[data-step="profile"] a')!;
    expect(profileLink.getAttribute('href')).toBe('/profile/live/pf_b#account-profile');
    // The first step still yours is the primary action.
    expect(el.querySelector('li[data-step="stage"] a')!.classList).toContain('btn-primary');
    expect(el.querySelector('li[data-step="profile"] a')!.classList).not.toContain('btn-primary');
    // No real money yet: a PAPER stamp and no brass.
    expect(el.querySelector('app-page-header app-mode-stamp')!.textContent).toContain('PAPER');
    expect(el.querySelector('ol.steps')!.classList).not.toContain('live');
  });

  it('wears the LIVE stamp only at a Real money stage', async () => {
    const el = await render();
    await answerBroker(true);
    expect(el.querySelector('app-page-header app-mode-stamp')!.textContent).toContain('LIVE');
    expect(el.querySelector('ol.steps')!.classList).toContain('live');
  });

  it('holds the later steps of a paper portfolio behind the broker step', async () => {
    const el = await render({ portfolio: 'pf_p' });
    expect(el.querySelector('li[data-step="broker"] .state')!.textContent).toContain('Not yet');
    expect(el.querySelector('li[data-step="stage"] .state')!.textContent).toContain(
      'Needs an earlier step',
    );
    expect(el.querySelector('app-page-header app-mode-stamp')).toBeNull();
    const picker = el.querySelector<HTMLSelectElement>('#gl-portfolio')!;
    expect(picker.value).toBe('pf_p');
  });
});
