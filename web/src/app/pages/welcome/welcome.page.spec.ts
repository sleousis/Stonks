import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import type { OnboardingView } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { SessionService } from '../../core/auth/session.service';
import { ADMIN, TRADER } from '../../../testing/auth-fixtures';
import { nextRequest, page, tick } from '../../../testing/http';
import { book } from '../../../testing/portfolio-fixtures';
import { ToastService } from '../../core/notify/toast.service';
import { WelcomePage } from './welcome.page';
import { currentStep, parseTickerText, progressText, setupComplete } from './welcome-steps';

const IDS = ['account', 'portfolio', 'data', 'follow', 'alerts'] as const;

function guide(states: Partial<Record<(typeof IDS)[number], 'done' | 'skipped'>> = {}) {
  const steps = IDS.map((id) => ({
    id,
    state: states[id] ?? ('todo' as const),
    derived: id === 'account' && states[id] === 'done',
    optional: id === 'portfolio' || id === 'data',
  }));
  const complete = steps.every((s) => s.state !== 'todo');
  return { steps, complete, dismissed: false, show: !complete } satisfies OnboardingView;
}

describe('welcome steps', () => {
  it('takes which steps may wait from the server', () => {
    const v = guide({ account: 'done', follow: 'done' });
    // The server marks alerts optional too: the guide is then complete.
    const steps = v.steps.map((s) => (s.id === 'alerts' ? { ...s, optional: true } : s));
    expect(progressText(steps)).toBe('2 of 2 done');
    expect(setupComplete(steps)).toBe(true);
    expect(setupComplete(v.steps)).toBe(false);
  });

  it('opens the first step still to do and counts progress', () => {
    const v = guide({ account: 'done', portfolio: 'skipped' });
    // Follow and alerts come before the optional steps (FR-01).
    expect(currentStep(v)).toBe('follow');
    expect(currentStep(guide({ account: 'done', follow: 'done', alerts: 'done' }))).toBe(
      'portfolio',
    );
    // Progress counts the steps that matter, never the optional ones (FR-01).
    expect(progressText(v.steps)).toBe('1 of 3 done');
    expect(progressText(guide({ account: 'done', alerts: 'skipped' }).steps)).toBe(
      '1 of 3 done, 1 skipped',
    );
    expect(currentStep(guide(Object.fromEntries(IDS.map((i) => [i, 'done']))))).toBeNull();
  });

  it('reads typed tickers once each, upper-cased', () => {
    expect(parseTickerText(' aapl.us, msft.us\nAAPL.US ;  ')).toEqual(['AAPL.US', 'MSFT.US']);
  });
});

describe('WelcomePage', () => {
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [provideRouter([]), ...provideApi(), provideHttpClientTesting()],
    });
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  async function render(
    me = TRADER,
    view = guide({ account: 'done' }),
    step?: string,
    books: unknown[] = [],
    strategyRows: unknown[] = [],
  ) {
    const session = TestBed.inject(SessionService);
    const loading = session.load();
    (await nextRequest(http, '/api/auth/me')).flush(me);
    await loading;
    const fixture = TestBed.createComponent(WelcomePage);
    if (step) fixture.componentRef.setInput('step', step);
    fixture.detectChanges();
    (await nextRequest(http, '/api/portfolios')).flush(page(books));
    (await nextRequest(http, '/api/onboarding')).flush(view);
    if (me.role === 'admin') {
      (await nextRequest(http, '/api/onboarding/system')).flush({
        complete: false,
        checks: [
          { id: 'data_source', done: true, detail: 'eodhd is set up and is the default' },
          { id: 'first_ingest', done: false, detail: 'no prices loaded yet' },
          { id: 'backup', done: false, detail: 'no backup on disk yet' },
          { id: 'scheduler', done: true, detail: 'heartbeat 12s ago' },
        ],
      });
    }
    const strategies = http.match((r) => r.url.split('?')[0] === '/api/strategies');
    const flushStrategies = (r: { request: { urlWithParams: string }; flush(b: unknown): void }) =>
      r.flush(page(r.request.urlWithParams.includes('status=active') ? strategyRows : []));
    for (const r of strategies) flushStrategies(r);
    if (!strategies.length) {
      await tick(5);
      for (const r of http.match((x) => x.url.split('?')[0] === '/api/strategies')) {
        flushStrategies(r);
      }
    }
    await tick(5);
    fixture.detectChanges();
    return fixture;
  }

  function buttonNamed(el: HTMLElement, text: string): HTMLButtonElement {
    const b = [...el.querySelectorAll('button')].find((x) => x.textContent?.trim() === text);
    if (!b) throw new Error(`no "${text}" button`);
    return b;
  }

  it('follows on the portfolio the select shows, and names the strategy in words', async () => {
    const books = [
      book({ id: 'pf_a', name: 'Alpha', is_default: true }),
      book({ id: 'pf_b', name: 'Beta' }),
    ];
    const row = { id: 'momentum_3fa9c21b', status: 'active', class_path: 'x:Momentum' };
    const fixture = await render(TRADER, guide({ account: 'done' }), 'follow', books, [row]);
    const el: HTMLElement = fixture.nativeElement;
    const choose = (sel: string, value: string) => {
      const s = el.querySelector<HTMLSelectElement>(sel)!;
      s.value = value;
      s.dispatchEvent(new Event('change'));
      fixture.detectChanges();
    };
    const radio = (text: string) =>
      [...el.querySelectorAll<HTMLLabelElement>('label.mode')]
        .find((l) => l.textContent?.includes(text))!
        .querySelector('input')!;
    choose('#wf-strategy', 'momentum_3fa9c21b');
    radio('Paper').click();
    fixture.detectChanges();
    choose('#wf-book', 'pf_b');
    radio('Alerts only').click();
    fixture.detectChanges();
    radio('Paper').click();
    fixture.detectChanges();
    const shown = el.querySelector<HTMLSelectElement>('#wf-book')!.value;
    const toast = vi.spyOn(TestBed.inject(ToastService), 'success');
    el.querySelector<HTMLFormElement>('form.inline-form')!.dispatchEvent(new Event('submit'));
    const req = await nextRequest(http, '/api/subscriptions', 'POST');
    expect(req.request.body.portfolio_id).toBe(shown);
    req.flush({});
    await tick(5);
    expect(toast.mock.calls[0]?.[0]).not.toContain('momentum_3fa9c21b');
    for (const r of http.match((x) => x.url.split('?')[0] === '/api/onboarding')) r.flush(guide());
  });

  it('lists the five steps and opens the first one still to do', async () => {
    const fixture = await render();
    const el: HTMLElement = fixture.nativeElement;
    const titles = [...el.querySelectorAll('.step-title')].map((t) => t.textContent?.trim());
    expect(titles).toEqual([
      'Protect your account',
      'Follow a strategy',
      'Turn on alerts',
      'Open a paper portfolio',
      'Choose what to watch',
    ]);
    expect(el.querySelector('.step.open .step-title')?.textContent).toContain('Follow a strategy');
    const tags = [...el.querySelectorAll('.step .tag')].map((t) => t.textContent?.trim());
    expect(tags).toEqual(['Done', 'To do', 'To do', 'Optional', 'Optional']);
    expect(el.querySelector('.standfirst')?.textContent).toBe(
      '1 of 3 done. Next: follow a strategy.',
    );
    // Closed steps say what they are for in one sentence.
    expect(el.querySelectorAll('.step-lead').length).toBe(4);
    expect(el.textContent).not.toContain('Your install');
  });

  it('opens the step named in the link, so "Open a paper portfolio" lands on it (UX-13)', async () => {
    const fixture = await render(
      TRADER,
      guide({ account: 'done', portfolio: 'skipped' }),
      'portfolio',
    );
    const el: HTMLElement = fixture.nativeElement;
    expect(el.querySelector('.step.open .step-title')?.textContent).toContain(
      'Open a paper portfolio',
    );
    expect(el.querySelector('#wp-name')).not.toBeNull();
  });

  it('ignores an unknown step in the link', async () => {
    const fixture = await render(TRADER, guide({ account: 'done' }), 'nope');
    const el: HTMLElement = fixture.nativeElement;
    expect(el.querySelector('.step.open .step-title')?.textContent).toContain('Follow a strategy');
  });

  it('skips a step and keeps the choice on the server', async () => {
    const fixture = await render(TRADER, guide({ account: 'done' }), 'portfolio');
    const el: HTMLElement = fixture.nativeElement;
    buttonNamed(el, 'Skip this step').click();
    const req = await nextRequest(http, '/api/onboarding/steps/portfolio', 'PUT');
    expect(req.request.body).toEqual({ state: 'skipped' });
    req.flush(guide({ account: 'done', portfolio: 'skipped' }));
    await tick();
    fixture.detectChanges();
    expect(el.querySelector('.step.open .step-title')?.textContent).toContain('Follow a strategy');
    expect(el.querySelector('.step[data-state="skipped"] .tag')?.textContent).toContain('Skipped');
  });

  it('saves a watchlist from the data step', async () => {
    const fixture = await render(TRADER, guide({ account: 'done', portfolio: 'done' }), 'data');
    const el: HTMLElement = fixture.nativeElement;
    // A saved watchlist ticks the step itself, so there is no "Mark as done" (FR-01).
    expect([...el.querySelectorAll('button')].map((b) => b.textContent?.trim())).not.toContain(
      'Mark as done',
    );
    const tickers = el.querySelector<HTMLTextAreaElement>('#wd-tickers')!;
    tickers.value = 'aapl.us msft.us';
    tickers.dispatchEvent(new Event('input'));
    fixture.detectChanges();
    buttonNamed(el, 'Save watchlist').click();
    const req = await nextRequest(http, '/api/watchlists', 'POST');
    expect(req.request.body).toEqual({ name: 'My watchlist', tickers: ['AAPL.US', 'MSFT.US'] });
    req.flush({
      id: 'wl_1',
      name: 'My watchlist',
      tickers: ['AAPL.US', 'MSFT.US'],
      created_at: '2026-09-27T00:00:00Z',
      updated_at: '2026-09-27T00:00:00Z',
    });
    (await nextRequest(http, '/api/watchlists')).flush(page([]));
    (await nextRequest(http, '/api/onboarding')).flush(
      guide({ account: 'done', portfolio: 'done', data: 'done' }),
    );
    await tick();
    fixture.detectChanges();
    expect(el.querySelector('.step.open .step-title')?.textContent).toContain('Follow a strategy');
  });

  it('shows admins whether the install is ready, with a way to fix each gap', async () => {
    const fixture = await render(ADMIN);
    const el: HTMLElement = fixture.nativeElement;
    expect(el.textContent).toContain('Your install');
    expect(el.textContent).toContain('Needs attention');
    const fixes = [...el.querySelectorAll('.check-fix')].map((a) => a.getAttribute('href'));
    expect(fixes).toEqual(['/data', '/ops/schedule']);
  });

  it('opens a paper portfolio when you follow on Paper without one (FR-01)', async () => {
    const row = { id: 'momentum_3fa9c21b', status: 'active', class_path: 'x:Momentum' };
    const fixture = await render(TRADER, guide({ account: 'done' }), 'follow', [], [row]);
    const el: HTMLElement = fixture.nativeElement;
    const select = el.querySelector<HTMLSelectElement>('#wf-strategy')!;
    select.value = 'momentum_3fa9c21b';
    select.dispatchEvent(new Event('change'));
    expect(select.options[1].textContent?.trim()).toBe('Momentum 3fa9 (Approved)');
    [...el.querySelectorAll<HTMLLabelElement>('label.mode')]
      .find((l) => l.textContent?.includes('Paper'))!
      .querySelector('input')!
      .click();
    fixture.detectChanges();
    el.querySelector<HTMLFormElement>('form.inline-form')!.dispatchEvent(new Event('submit'));
    const made = await nextRequest(http, '/api/portfolios', 'POST');
    expect(made.request.body).toEqual({ name: 'Paper portfolio', initial_cash: 100000 });
    made.flush(book({ id: 'pf_new', name: 'Paper portfolio' }));
    (await nextRequest(http, '/api/portfolios')).flush(
      page([book({ id: 'pf_new', name: 'Paper portfolio' })]),
    );
    const req = await nextRequest(http, '/api/subscriptions', 'POST');
    expect(req.request.body).toEqual({
      strategy_id: 'momentum_3fa9c21b',
      mode: 'paper',
      portfolio_id: 'pf_new',
    });
    req.flush({});
    await tick(5);
    for (const r of http.match((x) => x.url.split('?')[0] === '/api/onboarding')) r.flush(guide());
  });

  it('offers this device and Telegram as two equal choices for alerts (FR-07)', async () => {
    const fixture = await render(TRADER, guide({ account: 'done', follow: 'done' }), 'alerts');
    (await nextRequest(http, '/api/telegram/link')).flush({
      bot_configured: true,
      bot_enabled: true,
      bot_username: 'stonks_bot',
      linked: false,
      linked_at: null,
      username: null,
    });
    await tick(5);
    fixture.detectChanges();
    const el: HTMLElement = fixture.nativeElement;
    const heads = [...el.querySelectorAll('.channels h3')].map((h) => h.textContent?.trim());
    expect(heads).toEqual(['This device', 'Telegram']);
    expect(el.textContent).not.toContain('I turned them on');
    expect(el.textContent).toContain('Get a link code');
  });

  it('is done once follow and alerts are, the optional steps may wait', async () => {
    const fixture = await render(
      TRADER,
      guide({ account: 'done', follow: 'done', alerts: 'done' }),
    );
    expect(fixture.nativeElement.textContent).toContain('You are set up');
  });

  it('says you are set up when every step is handled', async () => {
    const all = guide(Object.fromEntries(IDS.map((i) => [i, 'done'])));
    const fixture = await render(TRADER, all);
    const el: HTMLElement = fixture.nativeElement;
    expect(el.textContent).toContain('You are set up');
  });
});
