import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { type ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import { provideApi } from '../../api/provide-api';
import { SessionService } from '../../core/auth/session.service';
import { StepUpService } from '../../core/auth/step-up.service';
import { ConfirmService } from '../../core/confirm/confirm.service';
import { ToastService } from '../../core/notify/toast.service';
import { MODES } from '../../shared/governance-labels';
import { TRADER } from '../../../testing/auth-fixtures';
import { nextRequest, tick } from '../../../testing/http';
import { StrategiesCard } from './strategies-card';
import { sub } from '../../../testing/home-fixtures';

describe('StrategiesCard', () => {
  let fixture: ComponentFixture<StrategiesCard>;
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

  async function render(list: object | null, status = 200) {
    fixture = TestBed.createComponent(StrategiesCard);
    fixture.detectChanges();
    const req = await nextRequest(controller, '/api/subscriptions');
    const items = Array.isArray(list) ? list : [];
    if (status === 200) req.flush({ items, total: items.length, limit: 500, offset: 0 });
    else req.flush({ title: 'x', status }, { status, statusText: 'x' });
    await tick();
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  function radio(el: HTMLElement, value: string) {
    return el.querySelector<HTMLInputElement>(`input[type="radio"][value="${value}"]`)!;
  }

  it('shows each strategy with its switch and mode', async () => {
    const el = await render([sub({ paper_days_completed: 12 })]);
    expect(el.textContent).toContain('momentum-v3');
    expect(el.querySelector('[role="switch"]')!.getAttribute('aria-checked')).toBe('true');
    expect(radio(el, 'paper').checked).toBe(true);
  });

  it('keeps auto disabled with the reason until 20 paper days', async () => {
    const el = await render([sub({ paper_days_completed: 12 })]);
    expect(radio(el, 'auto').disabled).toBe(true);
    expect(el.textContent).toContain('Auto unlocks after 20 paper trading days. 12 of 20 done.');
  });

  it('switches notify and paper straight away', async () => {
    const el = await render([sub({ mode: 'notify', paper_days_completed: 0 })]);
    radio(el, 'paper').click();
    const req = await nextRequest(controller, '/api/subscriptions/sub_1', 'PATCH');
    expect(req.request.body).toEqual({ mode: 'paper' });
    req.flush(sub({ mode: 'paper', paper_days_completed: 0 }));
    await tick();
    fixture.detectChanges();
    expect(el.textContent).toContain(MODES[1].help);
  });

  it('turns a strategy off', async () => {
    const el = await render([sub()]);
    el.querySelector<HTMLButtonElement>('[role="switch"]')!.click();
    const req = await nextRequest(controller, '/api/subscriptions/sub_1', 'PATCH');
    expect(req.request.body).toEqual({ enabled: false });
    req.flush(sub({ enabled: false }));
    await tick();
    fixture.detectChanges();
    expect(el.querySelector('[role="switch"]')!.getAttribute('aria-checked')).toBe('false');
  });

  it('asks for the step-up and a typed confirm before auto', async () => {
    const stepUp = vi.spyOn(TestBed.inject(StepUpService), 'ensure').mockResolvedValue(true);
    const confirm = vi.spyOn(TestBed.inject(ConfirmService), 'confirm').mockResolvedValue(true);
    const el = await render([sub()]);
    radio(el, 'auto').click();
    const req = await nextRequest(controller, '/api/subscriptions/sub_1', 'PATCH');
    expect(stepUp).toHaveBeenCalled();
    expect(confirm).toHaveBeenCalledWith(
      expect.objectContaining({ typedConfirmation: 'momentum-v3', tone: 'danger' }),
    );
    expect(req.request.body).toEqual({ mode: 'auto' });
    req.flush(sub({ mode: 'auto' }));
    await tick();
  });

  it('sends nothing and restores the mode when the step-up is cancelled', async () => {
    vi.spyOn(TestBed.inject(StepUpService), 'ensure').mockResolvedValue(false);
    const el = await render([sub()]);
    radio(el, 'auto').click();
    await tick();
    controller.expectNone('/api/subscriptions/sub_1');
    expect(radio(el, 'paper').checked).toBe(true);
  });

  it('auto confirm passes ticket.live true (UX-14)', async () => {
    vi.spyOn(TestBed.inject(StepUpService), 'ensure').mockResolvedValue(true);
    const confirm = vi.spyOn(TestBed.inject(ConfirmService), 'confirm').mockResolvedValue(false);
    const el = await render([sub()]);
    radio(el, 'auto').click();
    await tick();
    const options = confirm.mock.calls[0][0];
    expect(options.ticket?.live).toBe(true);
    expect(options.ticket?.lines.map((l) => l.label)).toEqual(['Strategy', 'Portfolio', 'Mode']);
    expect(options.ticket?.lines[2].value).toBe('Auto');
    controller.expectNone('/api/subscriptions/sub_1');
  });

  it('re-enabling an auto subscription asks for step-up and confirm, cancel sends no PATCH (UX-02)', async () => {
    const stepUp = vi.spyOn(TestBed.inject(StepUpService), 'ensure').mockResolvedValue(true);
    const confirm = vi.spyOn(TestBed.inject(ConfirmService), 'confirm').mockResolvedValue(false);
    const el = await render([sub({ mode: 'auto', enabled: false })]);
    const toggle = el.querySelector<HTMLButtonElement>('[role="switch"]')!;
    toggle.click();
    await tick();
    fixture.detectChanges();
    expect(stepUp).toHaveBeenCalled();
    expect(confirm).toHaveBeenCalledWith(
      expect.objectContaining({ ticket: expect.objectContaining({ live: true }) }),
    );
    controller.expectNone('/api/subscriptions/sub_1');
    expect(toggle.disabled).toBe(false);
    expect(toggle.getAttribute('aria-checked')).toBe('false');

    confirm.mockResolvedValue(true);
    toggle.click();
    const req = await nextRequest(controller, '/api/subscriptions/sub_1', 'PATCH');
    expect(req.request.body).toEqual({ enabled: true });
    req.flush(sub({ mode: 'auto', enabled: true }));
    await tick();
  });

  it('turning a paused auto subscription off needs no step-up', async () => {
    const stepUp = vi.spyOn(TestBed.inject(StepUpService), 'ensure');
    const el = await render([sub({ mode: 'auto', enabled: true })]);
    el.querySelector<HTMLButtonElement>('[role="switch"]')!.click();
    const req = await nextRequest(controller, '/api/subscriptions/sub_1', 'PATCH');
    expect(stepUp).not.toHaveBeenCalled();
    req.flush(sub({ mode: 'auto', enabled: false }));
    await tick();
  });

  it('copes with a cancelled server step-up: no stuck busy state, no success toast', async () => {
    const prompt = vi.spyOn(TestBed.inject(StepUpService), 'prompt').mockResolvedValue(false);
    const success = vi.spyOn(TestBed.inject(ToastService), 'success');
    const el = await render([sub()]);
    const toggle = el.querySelector<HTMLButtonElement>('[role="switch"]')!;
    toggle.click();
    (await nextRequest(controller, '/api/subscriptions/sub_1', 'PATCH')).flush(
      { title: 'Step-up needed', status: 403, code: 'step_up_required' },
      { status: 403, statusText: 'Forbidden' },
    );
    await tick(5);
    fixture.detectChanges();
    expect(prompt).toHaveBeenCalled();
    expect(success).not.toHaveBeenCalled();
    expect(toggle.disabled).toBe(false);
    expect(toggle.getAttribute('aria-checked')).toBe('true');
  });

  it('two toggles: after A resolves, B stays disabled (UX-59)', async () => {
    const el = await render([sub(), sub({ id: 'sub_2', strategy_id: 'value_1a2b3c4d' })]);
    const [a, b] = [...el.querySelectorAll<HTMLButtonElement>('[role="switch"]')];
    a.click();
    const reqA = await nextRequest(controller, '/api/subscriptions/sub_1', 'PATCH');
    b.click();
    const reqB = await nextRequest(controller, '/api/subscriptions/sub_2', 'PATCH');
    reqA.flush(sub({ enabled: false }));
    await tick();
    fixture.detectChanges();
    expect(b.disabled).toBe(true);
    reqB.flush(sub({ id: 'sub_2', strategy_id: 'value_1a2b3c4d', enabled: false }));
    await tick();
    fixture.detectChanges();
    expect(b.disabled).toBe(false);
  });

  it('names strategies and modes in trader words (UX-27, UX-31)', async () => {
    const el = await render([sub({ strategy_id: 'value_1a2b3c4d', strategy_status: 'shadow' })]);
    expect(el.querySelector('.name a')!.textContent!.trim()).toBe('Value 1a2b');
    expect(el.textContent).toContain('Paper trading');
    expect(el.textContent).not.toMatch(/shadow/i);
    const labels = [...el.querySelectorAll('.mode')].map((m) => m.textContent!.trim());
    expect(labels).toEqual(MODES.map((m) => m.label));
  });

  it('offers a retry when the list cannot load', async () => {
    const el = await render(null, 500);
    expect(el.textContent).toContain('Could not load your strategies');
    expect(el.textContent).not.toContain('Coming soon');
  });

  it('says how to start when the trader follows nothing', async () => {
    const el = await render([]);
    expect(el.textContent).toContain('You follow no strategies yet');
    const follow = [...el.querySelectorAll('a')].filter((a) =>
      a.textContent?.includes('Follow a strategy'),
    );
    expect(follow.length).toBe(2);
    expect(follow[1].getAttribute('href')).toBe('/strategies');
  });

  it('shows a viewer why the switches are off', async () => {
    const session = TestBed.inject(SessionService);
    const loading = session.load(true);
    (await nextRequest(controller, '/api/auth/me')).flush({
      ...TRADER,
      role: 'viewer',
      scopes: ['read'],
    });
    await loading;
    const el = await render([sub()]);
    expect(el.querySelector<HTMLButtonElement>('[role="switch"]')!.disabled).toBe(true);
    expect(el.querySelector<HTMLFieldSetElement>('fieldset.modes')!.disabled).toBe(true);
    expect(el.textContent).toContain('Traders and admins only.');
  });
});
