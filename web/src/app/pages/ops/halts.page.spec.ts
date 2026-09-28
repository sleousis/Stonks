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
import { StopTradingService } from '../../core/halts/stop-trading.service';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import { ADMIN, TRADER } from '../../../testing/auth-fixtures';
import { nextRequest, page, tick } from '../../../testing/http';
import { answerDialog } from '../../../testing/status-dialog';
import { HaltsPage } from './halts.page';
import { book } from '../../../testing/portfolio-fixtures';

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
    // A read can start another (the page follows the app-wide halt state), so
    // keep answering until the page is quiet.
    for (let round = 0; round < 5 && pending.length; round++) {
      await tick(5);
      pending = [...pending, ...http.match(isHalts)];
      for (const req of pending) {
        const everything = req.request.urlWithParams.includes('include_cleared=true');
        req.flush(page(everything ? all : all.filter((h) => h.active)));
      }
      await settle();
      pending = http.match(isHalts);
    }
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
      expect(active.textContent).toContain('Trading stopped');
      expect(active.textContent).toContain('Stop trading');
      expect(active.textContent).toContain('Broker outage');
      // No portfolio names known to an admin here: still no ids (UX-17).
      expect(active.textContent).toContain('One portfolio');
      expect(active.textContent).not.toContain('pf_default');
      // "Stops: New buys", not "Buys only" (UX-66).
      expect(active.textContent).toContain('New buys');
      expect(active.textContent).not.toContain('Buys only');
      expect(button('Resume trading')).toBeDefined();
      expect(button('Clear')).toBeDefined();

      const past = el.querySelector('[aria-labelledby="past-title"]')!;
      expect(past.textContent).toContain('Operational');
      expect(past.textContent).toContain('service:health');
      expect(past.textContent).toContain('health passed');
    });

    it('opens the one Stop trading sheet, never an inline form (M7)', () => {
      const open = TestBed.inject(StopTradingService).open;
      expect(el.querySelector('#kill-reason')).toBeNull();
      expect(button('Engage kill switch')).toBeUndefined();
      const stop = button('Stop trading…')!;
      expect(stop.classList).toContain('btn-danger');
      stop.click();
      expect(open()).toBe(true);
      expect(http.match('/api/halts/kill')).toEqual([]);
    });

    it('reloads when the app-wide halt state changes, e.g. Stop trading in the strip (UX-45)', async () => {
      const fromStrip = halt({ id: 20, reason: 'From the strip', tripped_by: 'usr_admin' });
      TestBed.inject(HaltStateService).add(fromStrip);
      TestBed.tick();
      await flushAll([KILL, BREAKER, OLD, fromStrip]);
      expect(el.querySelector('[aria-labelledby="active-title"]')!.textContent).toContain(
        'From the strip',
      );
    });

    it('turns the page head red while a kill switch is on (UX-51)', () => {
      expect(el.querySelector('app-page-header')!.classList).toContain('kill-on');
    });

    it('resumes only with the typed words and after a step-up', async () => {
      button('Resume trading')!.click();
      await settle();
      const form = el.querySelector<HTMLFormElement>('app-resume-sheet form')!;
      // A ticket: scope, what starts again, and the stamp.
      const ticket = form.querySelector('[aria-label="Resume trading"]')!;
      expect(ticket.textContent).toContain('Every portfolio');
      expect(ticket.textContent).toContain('All new orders');
      expect(ticket.textContent).toContain('Broker outage');
      expect(ticket.querySelector('.stamp')!.textContent).toContain('PAPER');
      const type = (sel: string, text: string) => {
        const input = form.querySelector<HTMLInputElement | HTMLTextAreaElement>(sel)!;
        input.value = text;
        input.dispatchEvent(new Event('input'));
        fixture.detectChanges();
      };
      const submit = form.querySelector<HTMLButtonElement>('button[type="submit"]')!;
      type('#resume-reason', 'Broker back');
      type('#resume-typed', 'resume');
      expect(submit.disabled).toBe(true);
      type('#resume-typed', 'RESUME TRADING');
      expect(submit.disabled).toBe(false);
      submit.click();
      fixture.detectChanges();

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
      book({ id: 'pf_default', name: 'Main book', is_default: true }),
      book({ id: 'pf_2', name: 'Crypto book' }),
    ];

    beforeEach(() => setup(TRADER, BOOKS));

    it('says the sheet starts on the portfolio on screen, and names portfolios', () => {
      const panel = el.querySelector('[aria-labelledby="kill-title"]')!;
      expect(panel.textContent).toContain('Main book');
      // Scope column names the portfolio, not its id.
      const active = el.querySelector('[aria-labelledby="active-title"]')!;
      expect(active.textContent).toContain('Portfolio Main book');
      expect(active.textContent).not.toContain('pf_default');
    });

    it('cannot resume a global kill switch but can clear its own breaker', () => {
      const resume = button('Resume trading')!;
      expect(resume.disabled).toBe(true);
      expect(resume.parentElement!.textContent).toContain('Admins only.');
      expect(button('Clear')!.disabled).toBe(false);
      expect(el.querySelector('[aria-labelledby="active-title"]')!.textContent).toContain(
        'Trading stopped',
      );
    });
  });

  describe('as a viewer', () => {
    beforeEach(() => setup({ ...TRADER, role: 'viewer', scopes: ['read'] }));

    it('sees why Stop trading and Clear are off', () => {
      const engage = button('Stop trading…')!;
      expect(engage.disabled).toBe(true);
      expect(engage.parentElement!.textContent).toContain('Traders and admins only.');
      expect(button('Clear')!.disabled).toBe(true);
    });
  });
});
