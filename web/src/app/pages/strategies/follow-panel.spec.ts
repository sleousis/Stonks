import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { type ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import { provideApi } from '../../api/provide-api';
import { SessionService } from '../../core/auth/session.service';
import { MODES } from '../../shared/governance-labels';
import { nextRequest, page, tick } from '../../../testing/http';
import { sub } from '../../../testing/home-fixtures';
import { book } from '../../../testing/portfolio-fixtures';
import { FollowPanel } from './follow-panel';

const MAIN = book({ id: 'pf_1', name: 'Main', is_default: true });
const SWING = book({ id: 'pf_2', name: 'Swing' });

describe('FollowPanel', () => {
  let fixture: ComponentFixture<FollowPanel>;
  let controller: HttpTestingController;
  let allowed: boolean;

  beforeEach(() => {
    allowed = true;
    TestBed.configureTestingModule({
      providers: [provideRouter([]), ...provideApi(), provideHttpClientTesting()],
    });
    controller = TestBed.inject(HttpTestingController);
    const session = TestBed.inject(SessionService);
    vi.spyOn(session, 'can').mockImplementation(() => allowed);
    vi.spyOn(session, 'whyNot').mockImplementation(() =>
      allowed ? null : 'Traders and admins only.',
    );
  });

  afterEach(() => controller.verify());

  async function render(subs = [sub({ strategy_id: 'other' })], books = [MAIN, SWING]) {
    fixture = TestBed.createComponent(FollowPanel);
    fixture.componentRef.setInput('strategyId', 'mom');
    fixture.detectChanges();
    (await nextRequest(controller, '/api/portfolios')).flush(page(books));
    (await nextRequest(controller, '/api/subscriptions')).flush(page(subs));
    await tick();
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  function button(el: HTMLElement, text: string) {
    return [...el.querySelectorAll('button')].find((b) => b.textContent?.trim() === text)!;
  }

  function pick(el: HTMLElement, value: string) {
    el.querySelector<HTMLInputElement>(`input[value="${value}"]`)!.click();
    fixture.detectChanges();
  }

  it('offers signals only or paper trading, never auto', async () => {
    const el = await render();
    const values = [...el.querySelectorAll<HTMLInputElement>('input[type=radio]')].map(
      (r) => r.value,
    );
    expect(values).toEqual(['notify', 'paper']);
    // The same words as Today's mode switch (UX-31).
    const labels = [...el.querySelectorAll('.mode strong')].map((s) => s.textContent!.trim());
    expect(labels).toEqual(
      MODES.filter((m) => m.value === 'notify' || m.value === 'paper').map((m) => m.label),
    );
    expect(el.textContent).not.toContain('Auto');
  });

  it('follows for signals only', async () => {
    const el = await render();
    button(el, 'Follow').click();
    const req = await nextRequest(controller, '/api/subscriptions', 'POST');
    expect(req.request.body).toEqual({ strategy_id: 'mom', mode: 'notify', portfolio_id: null });
    req.flush(sub({ strategy_id: 'mom', mode: 'notify', portfolio_id: null }));
    (await nextRequest(controller, '/api/subscriptions')).flush(
      page([sub({ strategy_id: 'mom', mode: 'notify', portfolio_id: null })]),
    );
    await tick();
    fixture.detectChanges();
    expect(el.textContent).toContain('You follow this strategy');
    expect(el.textContent).toContain('Signals only');
  });

  it('paper trades in the portfolio you pick', async () => {
    const el = await render();
    pick(el, 'paper');
    const select = el.querySelector<HTMLSelectElement>('#follow-portfolio')!;
    expect(select.value).toBe('pf_1');
    select.value = 'pf_2';
    select.dispatchEvent(new Event('change'));
    fixture.detectChanges();
    button(el, 'Follow').click();
    const req = await nextRequest(controller, '/api/subscriptions', 'POST');
    expect(req.request.body).toEqual({ strategy_id: 'mom', mode: 'paper', portfolio_id: 'pf_2' });
    req.flush(sub({ strategy_id: 'mom', portfolio_id: 'pf_2' }));
    (await nextRequest(controller, '/api/subscriptions')).flush(page([]));
    await tick();
  });

  it('asks for a portfolio first when you have none', async () => {
    const el = await render([], []);
    pick(el, 'paper');
    expect(el.textContent).toContain('You have no portfolio yet');
    expect(button(el, 'Follow').disabled).toBe(true);
  });

  it('says when you already follow it and points to Today', async () => {
    const el = await render([sub({ strategy_id: 'mom', portfolio_id: 'pf_1' })]);
    expect(el.textContent).toContain('Paper trading');
    expect(el.textContent).toContain('on Main');
    expect(el.querySelector('a[href="/"]')?.textContent).toContain('Change it on Today');
    expect(button(el, 'Follow')).toBeUndefined();
  });

  it('locks following without portfolio.trade', async () => {
    allowed = false;
    const el = await render();
    expect(button(el, 'Follow').disabled).toBe(true);
    expect(el.textContent).toContain('Traders and admins only.');
  });
});
