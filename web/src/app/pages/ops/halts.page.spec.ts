import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { computed, signal } from '@angular/core';
import { type ComponentFixture, TestBed } from '@angular/core/testing';

import type { HaltView, MeView } from '../../api/models';
import type { PortfolioRef } from '../../api/portfolios.service';
import { provideApi } from '../../api/provide-api';
import { SessionService } from '../../core/auth/session.service';
import { StepUpService } from '../../core/auth/step-up.service';
import { ConfirmService } from '../../core/confirm/confirm.service';
import { HaltStateService } from '../../core/halts/halt-state.service';
import { ToastService } from '../../core/notify/toast.service';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import { ADMIN, TRADER } from '../../../testing/auth-fixtures';
import { nextRequest, tick } from '../../../testing/http';
import { answerDialog, dialogForm, fillDialog } from '../../../testing/status-dialog';
import { HaltsPage } from './halts.page';

function halt(over: Partial<HaltView>): HaltView {
  return {
    id: 1,
    kind: 'kill',
    scope: 'global',
    user_id: null,
    portfolio_id: null,
    halt: 'all',
    reason: 'Broker outage',
    tripped_by: 'usr_owner',
    tripped_at: '2026-09-26T09:00:00Z',
    expires_on: null,
    cleared_at: null,
    cleared_by: null,
    clear_reason: null,
    active: true,
    ...over,
  };
}

const KILL = halt({});
const BREAKER = halt({
  id: 2,
  kind: 'drawdown',
  scope: 'portfolio',
  portfolio_id: 'pf_default',
  halt: 'buys',
  reason: 'Value 21% below peak',
  tripped_by: 'rule:circuit_breaker',
});
const OLD = halt({
  id: 3,
  kind: 'operational',
  reason: 'Stale data',
  active: false,
  cleared_at: '2026-09-20T10:00:00Z',
  cleared_by: 'service:health',
  clear_reason: 'health passed',
});

describe('HaltsPage', () => {
  let fixture: ComponentFixture<HaltsPage>;
  let http: HttpTestingController;
  let el: HTMLElement;
  let confirm: ReturnType<typeof vi.fn>;
  let stepUp: ReturnType<typeof vi.fn>;

  async function settle(): Promise<void> {
    for (let i = 0; i < 4; i++) {
      await tick();
      fixture.detectChanges();
    }
  }

  const isHalts = (r: { url: string }) => r.url.split('?')[0] === '/api/halts';

  /** Flush every pending GET /api/halts: the page reads all halts, the banner state the active ones. */
  async function flushAll(all: HaltView[]): Promise<void> {
    let pending = http.match(isHalts);
    for (let i = 0; i < 200 && pending.length === 0; i++) {
      await tick(1);
      pending = http.match(isHalts);
    }
    if (!pending.length) throw new Error('no GET /api/halts request');
    await tick(5);
    pending = [...pending, ...http.match(isHalts)];
    for (const req of pending) {
      const everything = req.request.urlWithParams.includes('include_cleared=true');
      req.flush(everything ? all : all.filter((h) => h.active));
    }
    await settle();
  }

  function button(text: string): HTMLButtonElement | undefined {
    return [...el.querySelectorAll('button')].find((b) => b.textContent?.trim() === text);
  }

  async function setup(me: MeView = ADMIN, portfolios: PortfolioRef[] = []): Promise<void> {
    confirm = vi.fn().mockResolvedValue(true);
    stepUp = vi.fn().mockResolvedValue(true);
    const options = signal(portfolios);
    TestBed.configureTestingModule({
      providers: [
        ...provideApi(),
        provideHttpClientTesting(),
        { provide: ConfirmService, useValue: { confirm } },
        { provide: StepUpService, useValue: { ensure: stepUp } },
        {
          provide: PortfolioContextService,
          useValue: {
            options,
            current: computed(() => options().find((p) => p.is_default) ?? options()[0] ?? null),
            load: () => Promise.resolve(),
          },
        },
      ],
    });
    http = TestBed.inject(HttpTestingController);
    const signingIn = TestBed.inject(SessionService).load();
    (await nextRequest(http, '/api/auth/me')).flush(me);
    await signingIn;
    fixture = TestBed.createComponent(HaltsPage);
    el = fixture.nativeElement;
    fixture.detectChanges();
    await flushAll([KILL, BREAKER, OLD]);
  }

  afterEach(() => http.verify());

  describe('as an admin', () => {
    beforeEach(() => setup(ADMIN));

    it('lists active halts with their way out, and past halts below', () => {
      const active = el.querySelector('[aria-labelledby="active-title"]')!;
      expect(active.textContent).toContain('Kill switch on');
      expect(active.textContent).toContain('Kill switch');
      expect(active.textContent).toContain('Broker outage');
      expect(active.textContent).toContain('Portfolio pf_default');
      expect(active.textContent).toContain('Buys only');
      expect(button('Resume trading')).toBeDefined();
      expect(button('Clear')).toBeDefined();

      const past = el.querySelector('[aria-labelledby="past-title"]')!;
      expect(past.textContent).toContain('Operational');
      expect(past.textContent).toContain('service:health');
      expect(past.textContent).toContain('health passed');
    });

    it('asks for a reason before engaging, then sends scope, portfolio and flatten', async () => {
      const success = vi.spyOn(TestBed.inject(ToastService), 'success');
      button('Engage kill switch')!.click();
      await settle();
      expect(el.textContent).toContain('Say why');
      expect(confirm).not.toHaveBeenCalled();

      el.querySelector<HTMLInputElement>('input[value="portfolio"]')!.click();
      fixture.detectChanges();
      const reason = el.querySelector<HTMLTextAreaElement>('#kill-reason')!;
      reason.value = 'Odd fills';
      reason.dispatchEvent(new Event('input'));
      const flatten = el.querySelector<HTMLInputElement>('.kill input[type="checkbox"]')!;
      flatten.click();
      fixture.detectChanges();

      button('Engage kill switch')!.click();
      const post = await nextRequest(http, '/api/halts/kill', 'POST');
      expect(confirm).toHaveBeenCalledWith(expect.objectContaining({ tone: 'danger' }));
      expect(post.request.body).toEqual({
        scope: 'portfolio',
        reason: 'Odd fills',
        flatten: true,
        portfolio_id: 'pf_default',
      });
      post.flush(halt({ id: 9, scope: 'portfolio', portfolio_id: 'pf_default', halt: 'buys' }));
      await flushAll([KILL, BREAKER, OLD]);
      expect(success).toHaveBeenCalledWith('Engaged the kill switch for portfolio pf_default.');
    });

    it('resumes only with the typed words and after a step-up', async () => {
      button('Resume trading')!.click();
      await settle();
      expect(dialogForm(el)!.textContent).toContain('RESUME TRADING');
      expect(fillDialog(fixture, { reason: 'Broker back', typed: 'resume' }).disabled).toBe(true);
      answerDialog(fixture, { reason: 'Broker back', typed: 'RESUME TRADING' });

      const post = await nextRequest(http, '/api/halts/1/resume', 'POST');
      expect(stepUp).toHaveBeenCalledWith('Resume trading');
      expect(post.request.body).toEqual({ confirmation: 'RESUME TRADING', reason: 'Broker back' });
      post.flush({ ...KILL, active: false, cleared_by: 'usr_owner' });
      await flushAll([BREAKER]);
      expect(button('Resume trading')).toBeUndefined();
      expect(TestBed.inject(HaltStateService).kills()).toEqual([]);
    });

    it('clears a breaker halt with a reason', async () => {
      button('Clear')!.click();
      await settle();
      answerDialog(fixture, { reason: 'Reviewed the drawdown' });
      const post = await nextRequest(http, '/api/halts/2/clear', 'POST');
      expect(post.request.body).toEqual({ reason: 'Reviewed the drawdown' });
      post.flush({ ...BREAKER, active: false });
      await flushAll([KILL]);
      expect(button('Clear')).toBeUndefined();
    });
  });

  describe('as a trader', () => {
    const BOOKS: PortfolioRef[] = [
      { id: 'pf_default', name: 'Main book', is_default: true },
      { id: 'pf_2', name: 'Crypto book' },
    ];

    beforeEach(() => setup(TRADER, BOOKS));

    it('offers one portfolio by name, never every portfolio', async () => {
      expect(el.querySelector('input[value="global"]')).toBeNull();
      expect(el.querySelector<HTMLInputElement>('input[value="portfolio"]')!.checked).toBe(true);
      expect(el.textContent).toContain('Only admins can stop every portfolio at once.');
      const select = el.querySelector<HTMLSelectElement>('#kill-portfolio')!;
      expect([...select.options].map((o) => o.textContent?.trim())).toEqual([
        'Main book',
        'Crypto book',
      ]);
      // Scope column names the portfolio, not its id.
      const active = el.querySelector('[aria-labelledby="active-title"]')!;
      expect(active.textContent).toContain('Portfolio Main book');
      expect(active.textContent).not.toContain('pf_default');

      select.value = 'pf_2';
      select.dispatchEvent(new Event('change'));
      const reason = el.querySelector<HTMLTextAreaElement>('#kill-reason')!;
      reason.value = 'Odd fills';
      reason.dispatchEvent(new Event('input'));
      fixture.detectChanges();
      button('Engage kill switch')!.click();
      const post = await nextRequest(http, '/api/halts/kill', 'POST');
      expect(post.request.body).toEqual(
        expect.objectContaining({ scope: 'portfolio', portfolio_id: 'pf_2' }),
      );
      post.flush(halt({ id: 9, scope: 'portfolio', portfolio_id: 'pf_2' }));
      await flushAll([KILL, BREAKER, OLD]);
    });

    it('cannot resume a global kill switch but can clear its own breaker', () => {
      const resume = button('Resume trading')!;
      expect(resume.disabled).toBe(true);
      expect(resume.parentElement!.textContent).toContain('Admins only.');
      expect(button('Clear')!.disabled).toBe(false);
      expect(el.querySelector('[aria-labelledby="active-title"]')!.textContent).toContain(
        'Kill switch on',
      );
    });
  });

  describe('as a viewer', () => {
    beforeEach(() => setup({ ...TRADER, role: 'viewer', scopes: ['read'] }));

    it('sees why the kill switch and Clear are off', () => {
      const engage = button('Engage kill switch')!;
      expect(engage.disabled).toBe(true);
      expect(engage.parentElement!.textContent).toContain('Traders and admins only.');
      expect(button('Clear')!.disabled).toBe(true);
    });
  });
});
