import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { type ComponentFixture, TestBed } from '@angular/core/testing';

import type { GateDayView, GateReportView, LiveStageView } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { SessionService } from '../../core/auth/session.service';
import { StepUpService } from '../../core/auth/step-up.service';
import { ConfirmService } from '../../core/confirm/confirm.service';
import { checkState, dirtyReasons, lowerStages, stageWords } from '../../shared/live-stages';
import { nextRequest, tick } from '../../../testing/http';
import { LiveStageCard, gateFigures } from './live-stage-card';

const DAY: GateDayView = {
  session_date: '2026-09-25',
  stage: 'broker_paper',
  orders_sent: 4,
  orders_filled: 3,
  orders_rejected: 1,
  orders_refused: 0,
  stuck_orders: 1,
  fills: 3,
  fills_missing_commission: 0,
  tca_orders: 3,
  tca_gap_bps: 2.5,
  live_return: 0.01,
  model_return: 0.008,
  drift_items: 0,
  reject_rate: 0.25,
  clean: false,
};

const STAGE: LiveStageView = {
  portfolio_id: 'pf_live',
  stage: 'broker_paper',
  next_stage: 'live_small',
  real_money: false,
  history: [
    {
      id: 1,
      from_stage: 'sim_paper',
      to_stage: 'broker_paper',
      direction: 'promote',
      actor: 'user:u1',
      reason: 'soak starts',
      gate_report: { passed: true },
      created_at: '2026-08-03T12:00:00Z',
    },
  ],
  days: [{ ...DAY, session_date: '2026-09-24', clean: true, stuck_orders: 0, reject_rate: 0 }, DAY],
};

function report(passed: boolean): GateReportView {
  return {
    portfolio_id: 'pf_live',
    from_stage: 'broker_paper',
    target: 'live_small',
    passed,
    checks: [
      { name: 'sessions', passed: true, detail: '20 session(s) recorded', value: 20, required: 20 },
      { name: 'clean_sessions', passed, detail: 'the last 20 session(s) were clean' },
      { name: 'kill_switch_drill', passed: null, detail: 'unavailable: drills come later' },
    ],
    metrics: { sessions: 20, clean_streak: 20, reject_rate: 0.01, tca_gap_bps: 1.25 },
    computed_at: '2026-09-27T20:00:00Z',
  };
}

describe('live stage words', () => {
  it('names the stages and what moving down may reach', () => {
    expect(stageWords('live_small')).toMatchObject({ label: 'Real money, small', live: true });
    expect(stageWords('broker_paper').live).toBe(false);
    expect(lowerStages('live_small')).toEqual(['sim_paper', 'broker_paper']);
    expect(lowerStages('sim_paper')).toEqual([]);
  });

  it('reads a check as met, not met or no data yet', () => {
    expect([true, false, null].map((passed) => checkState({ passed }))).toEqual([
      'pass',
      'fail',
      'none',
    ]);
  });

  it('says why a session was not clean', () => {
    expect(dirtyReasons(DAY)).toEqual(['1 order(s) still open', '25% rejected']);
  });

  it('shows the gate figures, with none yet for missing ones', () => {
    const figures = gateFigures(report(true));
    expect(figures.find((f) => f.label === 'Cost gap')?.value).toBe('1.3 bps');
    expect(figures.find((f) => f.label === 'Tracking error')?.value).toBe('None yet');
  });
});

describe('LiveStageCard', () => {
  let fixture: ComponentFixture<LiveStageCard>;
  let http: HttpTestingController;
  let allowed: boolean;
  let confirm: ReturnType<typeof vi.spyOn>;
  let ensure: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    allowed = true;
    TestBed.configureTestingModule({ providers: [...provideApi(), provideHttpClientTesting()] });
    http = TestBed.inject(HttpTestingController);
    const session = TestBed.inject(SessionService);
    vi.spyOn(session, 'can').mockImplementation(() => allowed);
    vi.spyOn(session, 'whyNot').mockImplementation(() => (allowed ? null : 'Traders only.'));
    confirm = vi.spyOn(TestBed.inject(ConfirmService), 'confirm').mockResolvedValue(true);
    ensure = vi.spyOn(TestBed.inject(StepUpService), 'ensure').mockResolvedValue(true);
  });

  afterEach(() => http.verify());

  async function render(passed = true): Promise<HTMLElement> {
    fixture = TestBed.createComponent(LiveStageCard);
    fixture.componentRef.setInput('portfolioId', 'pf_live');
    fixture.componentRef.setInput('portfolioName', 'Main live');
    fixture.detectChanges();
    (await nextRequest(http, '/api/portfolios/pf_live/live/stage')).flush(STAGE);
    (await nextRequest(http, '/api/portfolios/pf_live/live/gate-report')).flush(report(passed));
    await tick();
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  function type(el: HTMLElement, selector: string, value: string) {
    const input = el.querySelector<HTMLTextAreaElement>(selector)!;
    input.value = value;
    input.dispatchEvent(new Event('input'));
  }

  function button(el: HTMLElement, text: string) {
    return [...el.querySelectorAll('button')].find((b) => b.textContent?.trim() === text)!;
  }

  it('shows the ladder with the current stage marked, and the checks', async () => {
    const el = await render();
    const here = el.querySelector('.step[aria-current="step"]');
    expect(here?.textContent).toContain('Broker paper');
    expect(el.querySelectorAll('.step.done').length).toBe(1);
    expect(el.textContent).toContain('To move up to Real money, small');
    const checks = [...el.querySelectorAll('.checks li')].map((li) => li.textContent ?? '');
    expect(checks.find((t) => t.includes('Stop trading tested'))).toContain('No data yet');
    expect(el.querySelector('.stage-note')?.textContent).toContain('never moves the stage');
    const days = [...el.querySelectorAll('.days li')].map((li) => li.textContent ?? '');
    expect(days[0]).toContain('Not clean');
    expect(days[0]).toContain('1 order(s) still open');
  });

  it('moves up after the ticket, the typed stage name and a fresh code', async () => {
    const el = await render();
    type(el, '#stage-up-reason', 'soak went well');
    fixture.detectChanges();
    button(el, 'Move up to Real money, small').click();
    const post = await nextRequest(http, '/api/portfolios/pf_live/live/stage/promote', 'POST');
    const options = confirm.mock.calls[0][0] as {
      typedConfirmation: string;
      tone: string;
      ticket: { live: boolean };
    };
    expect(options.typedConfirmation).toBe('Real money, small');
    expect(options.tone).toBe('danger');
    expect(options.ticket.live).toBe(true);
    expect(ensure).toHaveBeenCalledTimes(1);
    expect(post.request.body).toEqual({
      to_stage: 'live_small',
      reason: 'soak went well',
      confirm: 'live_small',
    });
    post.flush({ ...STAGE, stage: 'live_small', next_stage: 'live_scale', real_money: true });
    (await nextRequest(http, '/api/portfolios/pf_live/live/gate-report')).flush({
      ...report(false),
      from_stage: 'live_small',
      target: 'live_scale',
    });
    await tick();
    fixture.detectChanges();
    expect(el.querySelector('.step[aria-current="step"]')?.textContent).toContain(
      'Real money, small',
    );
  });

  it('cannot move up while a check is not met', async () => {
    const el = await render(false);
    expect(button(el, 'Move up to Real money, small').disabled).toBe(true);
    expect(el.textContent).toContain('Every check must be met first.');
  });

  it('needs a reason to move up', async () => {
    const el = await render();
    el.querySelector<HTMLFormElement>('form[aria-labelledby="move-up-title"]')!.dispatchEvent(
      new Event('submit'),
    );
    await tick();
    fixture.detectChanges();
    expect(el.textContent).toContain('Say why now.');
    expect(confirm).not.toHaveBeenCalled();
  });

  it('moves down with a reason and no code', async () => {
    const el = await render();
    el.querySelector<HTMLDetailsElement>('.move-down')!.open = true;
    type(el, '#stage-down-reason', 'bad week');
    fixture.detectChanges();
    button(el, 'Move down').click();
    const post = await nextRequest(http, '/api/portfolios/pf_live/live/stage/demote', 'POST');
    expect(post.request.body).toEqual({ to_stage: 'sim_paper', reason: 'bad week' });
    expect(ensure).not.toHaveBeenCalled();
    post.flush({ ...STAGE, stage: 'sim_paper', next_stage: 'broker_paper' });
    (await nextRequest(http, '/api/portfolios/pf_live/live/gate-report')).flush(report(false));
    await tick();
  });

  it('turns the controls off for someone who may not change the stage', async () => {
    allowed = false;
    const el = await render();
    expect(button(el, 'Move up to Real money, small').disabled).toBe(true);
    expect(button(el, 'Move down').disabled).toBe(true);
  });
});
