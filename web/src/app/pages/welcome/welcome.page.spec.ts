import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import type { OnboardingView } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { SessionService } from '../../core/auth/session.service';
import { ADMIN, TRADER } from '../../../testing/auth-fixtures';
import { nextRequest, page, tick } from '../../../testing/http';
import { WelcomePage } from './welcome.page';
import { currentStep, parseTickerText, progressText } from './welcome-steps';

const IDS = ['account', 'portfolio', 'data', 'follow', 'alerts'] as const;

function guide(states: Partial<Record<(typeof IDS)[number], 'done' | 'skipped'>> = {}) {
  const steps = IDS.map((id) => ({
    id,
    state: states[id] ?? ('todo' as const),
    derived: id === 'account' && states[id] === 'done',
  }));
  const complete = steps.every((s) => s.state !== 'todo');
  return { steps, complete, dismissed: false, show: !complete } satisfies OnboardingView;
}

describe('welcome steps', () => {
  it('opens the first step still to do and counts progress', () => {
    const v = guide({ account: 'done', portfolio: 'skipped' });
    expect(currentStep(v)).toBe('data');
    expect(progressText(v.steps)).toBe('1 of 5 done, 1 skipped');
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

  async function render(me = TRADER, view = guide({ account: 'done' })) {
    const session = TestBed.inject(SessionService);
    const loading = session.load();
    (await nextRequest(http, '/api/auth/me')).flush(me);
    await loading;
    const fixture = TestBed.createComponent(WelcomePage);
    fixture.detectChanges();
    (await nextRequest(http, '/api/portfolios')).flush(page([]));
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
    for (const r of strategies) r.flush(page([]));
    if (!strategies.length) {
      await tick(5);
      for (const r of http.match((x) => x.url.split('?')[0] === '/api/strategies')) {
        r.flush(page([]));
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

  it('lists the five steps and opens the first one still to do', async () => {
    const fixture = await render();
    const el: HTMLElement = fixture.nativeElement;
    const titles = [...el.querySelectorAll('.step-title')].map((t) => t.textContent?.trim());
    expect(titles).toEqual([
      'Protect your account',
      'Pick a portfolio',
      'Choose what to watch',
      'Follow a strategy',
      'Turn on alerts',
    ]);
    expect(el.querySelector('.step.open .step-title')?.textContent).toContain('Pick a portfolio');
    expect(el.textContent).toContain('1 of 5 done');
    expect(el.textContent).not.toContain('Your install');
  });

  it('skips a step and keeps the choice on the server', async () => {
    const fixture = await render();
    const el: HTMLElement = fixture.nativeElement;
    buttonNamed(el, 'Skip this step').click();
    const req = await nextRequest(http, '/api/onboarding/steps/portfolio', 'PUT');
    expect(req.request.body).toEqual({ state: 'skipped' });
    req.flush(guide({ account: 'done', portfolio: 'skipped' }));
    await tick();
    fixture.detectChanges();
    expect(el.querySelector('.step.open .step-title')?.textContent).toContain(
      'Choose what to watch',
    );
    expect(el.querySelector('.step[data-state="skipped"] .tag')?.textContent).toContain('Skipped');
  });

  it('saves a watchlist from the data step', async () => {
    const fixture = await render(TRADER, guide({ account: 'done', portfolio: 'done' }));
    const el: HTMLElement = fixture.nativeElement;
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

  it('says you are set up when every step is handled', async () => {
    const all = guide(Object.fromEntries(IDS.map((i) => [i, 'done'])));
    const fixture = await render(TRADER, all);
    const el: HTMLElement = fixture.nativeElement;
    expect(el.textContent).toContain('You are set up');
  });
});
