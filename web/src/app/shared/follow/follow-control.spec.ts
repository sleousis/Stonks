import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { Component, signal } from '@angular/core';
import { type ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import { provideApi } from '../../api/provide-api';
import type { SubscriptionView } from '../../api/subscriptions.service';
import { SessionService } from '../../core/auth/session.service';
import { StepUpService } from '../../core/auth/step-up.service';
import { ConfirmService } from '../../core/confirm/confirm.service';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import { MODES } from '../governance-labels';
import { TRADER } from '../../../testing/auth-fixtures';
import { sub } from '../../../testing/home-fixtures';
import { nextRequest, tick } from '../../../testing/http';
import { FollowControl } from './follow-control';

@Component({
  imports: [FollowControl],
  template: `<app-follow-control [sub]="s()" [strategyName]="name()" (changed)="last = $event"
    ><span class="label">{{ name() }}</span></app-follow-control
  >`,
})
class Host {
  readonly s = signal<SubscriptionView>(sub());
  readonly name = signal<string | null>('Momentum 3fa9');
  last: SubscriptionView | null = null;
}

describe('FollowControl (M8)', () => {
  let fixture: ComponentFixture<Host>;
  let controller: HttpTestingController;

  beforeEach(async () => {
    TestBed.configureTestingModule({
      providers: [provideRouter([]), ...provideApi(), provideHttpClientTesting()],
    });
    controller = TestBed.inject(HttpTestingController);
    const session = TestBed.inject(SessionService);
    const loading = session.load();
    (await nextRequest(controller, '/api/auth/me')).flush(TRADER);
    await loading;
  });

  afterEach(() => controller.verify());

  function render(s: SubscriptionView = sub(), name: string | null = 'Momentum 3fa9') {
    fixture = TestBed.createComponent(Host);
    fixture.componentInstance.s.set(s);
    fixture.componentInstance.name.set(name);
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  function radio(el: HTMLElement, value: string) {
    return el.querySelector<HTMLInputElement>(`input[type="radio"][value="${value}"]`)!;
  }

  function paperBook() {
    vi.spyOn(TestBed.inject(PortfolioContextService), 'options').mockReturnValue([
      { id: 'pf_1', name: 'Main', trading: 'paper', status: 'active' } as never,
    ]);
  }

  it('shows the four follow modes in the agreed words and the projected name', () => {
    const el = render();
    const labels = [...el.querySelectorAll('.mode')].map((m) => m.textContent!.trim());
    expect(labels).toEqual(['Alerts only', 'Paper', 'Approve each trade', 'Automatic']);
    expect(el.querySelector('.label')!.textContent).toBe('Momentum 3fa9');
    expect(radio(el, 'paper').checked).toBe(true);
    expect(el.textContent).toContain(MODES[1].help);
  });

  it('shows only Alerts only and Paper with one line until the gate opens (FR-03)', () => {
    const el = render(
      sub({
        paper_days_completed: 12,
        auto_blockers: ['auto mode needs a broker portfolio', '12 of 20 paper trading days'],
      }),
    );
    const labels = [...el.querySelectorAll('.mode')].map((m) => m.textContent!.trim());
    expect(labels).toEqual(['Alerts only', 'Paper']);
    expect(radio(el, 'auto')).toBeNull();
    expect(el.textContent).toContain(
      'More modes unlock with a broker portfolio after 20 paper days.',
    );
    expect(el.textContent).toContain('Your paper days: 12 of 20.');
    expect(el.textContent).not.toContain('Automatic needs');
  });

  it('an Alerts only follow shows no paper-day count', () => {
    const el = render(
      sub({
        mode: 'notify',
        portfolio_id: null,
        paper_days_completed: 0,
        auto_blockers: ['auto mode needs a portfolio', '0 of 20 paper trading days'],
      }),
    );
    expect(el.textContent).not.toContain('Paper days');
    expect(el.textContent).toContain('More modes unlock with a broker portfolio');
  });

  it('asks before Alerts only restarts the paper days (PO-01)', async () => {
    const confirm = vi.spyOn(TestBed.inject(ConfirmService), 'confirm').mockResolvedValue(false);
    const el = render(
      sub({ paper_days_completed: 10, auto_blockers: ['10 of 20 paper trading days'] }),
    );
    radio(el, 'notify').click();
    await tick();
    const options = confirm.mock.calls[0][0];
    expect(options.title).toBe('Switch to Alerts only?');
    expect(options.message).toBe('Your 10 paper days start again from 0.');
    expect(options.confirmLabel).toBe('Switch and restart');
    controller.expectNone('/api/subscriptions/sub_1');
    expect(radio(el, 'paper').checked).toBe(true);
  });

  it('switches to Alerts only after the restart is confirmed', async () => {
    vi.spyOn(TestBed.inject(ConfirmService), 'confirm').mockResolvedValue(true);
    const el = render(
      sub({ paper_days_completed: 3, auto_blockers: ['3 of 20 paper trading days'] }),
    );
    radio(el, 'notify').click();
    const req = await nextRequest(controller, '/api/subscriptions/sub_1', 'PATCH');
    expect(req.request.body).toEqual({ mode: 'notify' });
    req.flush(sub({ mode: 'notify', paper_days_completed: 0 }));
    await tick();
  });

  it('switches Alerts only and Paper straight away and reports the change', async () => {
    const el = render(sub({ mode: 'notify' }));
    radio(el, 'paper').click();
    const req = await nextRequest(controller, '/api/subscriptions/sub_1', 'PATCH');
    expect(req.request.body).toEqual({ mode: 'paper' });
    req.flush(sub({ mode: 'paper' }));
    await tick();
    fixture.detectChanges();
    expect(fixture.componentInstance.last?.mode).toBe('paper');
  });

  it('turns a follow off with the switch', async () => {
    const el = render();
    el.querySelector<HTMLButtonElement>('[role="switch"]')!.click();
    const req = await nextRequest(controller, '/api/subscriptions/sub_1', 'PATCH');
    expect(req.request.body).toEqual({ enabled: false });
    req.flush(sub({ enabled: false }));
    await tick();
    fixture.detectChanges();
    expect(el.querySelector('[role="switch"]')!.getAttribute('aria-checked')).toBe('false');
  });

  it('asks to type the display name, never the hidden id, before Automatic', async () => {
    vi.spyOn(TestBed.inject(StepUpService), 'ensure').mockResolvedValue(true);
    const confirm = vi.spyOn(TestBed.inject(ConfirmService), 'confirm').mockResolvedValue(true);
    const el = render(sub({ strategy_id: 'momentum_3fa9c21b' }), null);
    radio(el, 'auto').click();
    const req = await nextRequest(controller, '/api/subscriptions/sub_1', 'PATCH');
    const options = confirm.mock.calls[0][0];
    expect(options.typedConfirmation).toBe('Momentum 3fa9');
    expect(options.ticket?.lines.map((l) => l.value)).toContain('Automatic');
    req.flush(sub({ mode: 'auto' }));
    await tick();
  });

  it('matches the words to the stamp: paper money never says real orders or goes red', async () => {
    paperBook();
    vi.spyOn(TestBed.inject(StepUpService), 'ensure').mockResolvedValue(true);
    const confirm = vi.spyOn(TestBed.inject(ConfirmService), 'confirm').mockResolvedValue(false);
    const el = render();
    radio(el, 'auto').click();
    await tick();
    const options = confirm.mock.calls[0][0];
    expect(options.ticket?.live).toBe(false);
    expect(options.tone).toBe('default');
    expect(options.message).toContain('No real money moves');
    expect(options.message).not.toMatch(/real orders/i);
    expect(radio(el, 'paper').checked).toBe(true);
    controller.expectNone('/api/subscriptions/sub_1');
  });

  it('real money, or an unknown portfolio, is LIVE and red', async () => {
    vi.spyOn(TestBed.inject(StepUpService), 'ensure').mockResolvedValue(true);
    const confirm = vi.spyOn(TestBed.inject(ConfirmService), 'confirm').mockResolvedValue(false);
    const el = render();
    radio(el, 'auto').click();
    await tick();
    const options = confirm.mock.calls[0][0];
    expect(options.ticket?.live).toBe(true);
    expect(options.tone).toBe('danger');
    expect(options.message).toContain('real orders');
  });

  it('asks for a code and a ticket before Approve each trade, no typed name', async () => {
    vi.spyOn(TestBed.inject(StepUpService), 'ensure').mockResolvedValue(true);
    const confirm = vi.spyOn(TestBed.inject(ConfirmService), 'confirm').mockResolvedValue(true);
    const el = render();
    radio(el, 'approve').click();
    const req = await nextRequest(controller, '/api/subscriptions/sub_1', 'PATCH');
    expect(confirm.mock.calls[0][0].typedConfirmation).toBeUndefined();
    req.flush(sub({ mode: 'approve' }));
    await tick();
    fixture.detectChanges();
    expect(radio(el, 'approve').checked).toBe(true);
  });

  it('sends nothing and restores the mode when the code is cancelled', async () => {
    vi.spyOn(TestBed.inject(StepUpService), 'ensure').mockResolvedValue(false);
    const el = render();
    radio(el, 'auto').click();
    await tick();
    controller.expectNone('/api/subscriptions/sub_1');
    expect(radio(el, 'paper').checked).toBe(true);
  });

  it('says why a viewer cannot change it', async () => {
    const session = TestBed.inject(SessionService);
    const loading = session.load(true);
    (await nextRequest(controller, '/api/auth/me')).flush({
      ...TRADER,
      role: 'viewer',
      scopes: ['read'],
    });
    await loading;
    const el = render();
    expect(el.querySelector<HTMLButtonElement>('[role="switch"]')!.disabled).toBe(true);
    expect(el.querySelector<HTMLFieldSetElement>('fieldset.modes')!.disabled).toBe(true);
  });
});
