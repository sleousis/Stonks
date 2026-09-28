import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { type ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import type { ModelVersionView, SwapReportView } from '../../api/model-versions.service';
import { provideApi } from '../../api/provide-api';
import { SessionService } from '../../core/auth/session.service';
import { StepUpService } from '../../core/auth/step-up.service';
import { ToastService } from '../../core/notify/toast.service';
import { ADMIN, TRADER } from '../../../testing/auth-fixtures';
import { nextRequest, page, tick } from '../../../testing/http';
import {
  CANDIDATE_V2,
  LIVE_V1,
  MODEL_ID,
  swapReport,
  versionEvent,
} from '../../../testing/model-version-fixtures';
import { answerDialog, dialogForm, fillDialog } from '../../../testing/status-dialog';
import { ModelVersionsPanel } from './model-versions-panel';

const BASE = `/api/strategies/${MODEL_ID}/versions`;

describe('ModelVersionsPanel', () => {
  let fixture: ComponentFixture<ModelVersionsPanel>;
  let http: HttpTestingController;
  let el: HTMLElement;
  let ensure: ReturnType<typeof vi.fn>;

  async function settle() {
    for (let i = 0; i < 3; i++) {
      await tick();
      fixture.detectChanges();
    }
  }

  async function render(
    versions: ModelVersionView[],
    report: SwapReportView | null = null,
    me = ADMIN,
  ): Promise<void> {
    ensure = vi.fn().mockResolvedValue(true);
    TestBed.configureTestingModule({
      providers: [
        provideRouter([]),
        ...provideApi(),
        provideHttpClientTesting(),
        { provide: StepUpService, useValue: { ensure } },
      ],
    });
    http = TestBed.inject(HttpTestingController);
    const loading = TestBed.inject(SessionService).load();
    (await nextRequest(http, '/api/auth/me')).flush(me);
    await loading;
    fixture = TestBed.createComponent(ModelVersionsPanel);
    fixture.componentRef.setInput('strategyId', MODEL_ID);
    el = fixture.nativeElement;
    fixture.detectChanges();
    (await nextRequest(http, BASE)).flush(page(versions));
    (await nextRequest(http, `${BASE}/history`)).flush(
      page([
        versionEvent(),
        versionEvent({
          id: 2,
          version: 2,
          kind: 'candidate',
          to_status: 'candidate',
          actor: 'service:scheduler',
          created_at: '2026-09-20T06:00:00Z',
        }),
      ]),
    );
    await settle();
    if (report) {
      (await nextRequest(http, `${BASE}/2/check`)).flush(report);
      await settle();
    }
  }

  function button(text: string): HTMLButtonElement {
    const found = [...el.querySelectorAll<HTMLButtonElement>('button')].find(
      (b) => b.textContent!.trim() === text,
    );
    if (!found) throw new Error(`no button ${text}`);
    return found;
  }

  function reloads(versions: ModelVersionView[]) {
    return (async () => {
      (await nextRequest(http, BASE)).flush(page(versions));
      (await nextRequest(http, `${BASE}/history`)).flush(page([versionEvent()]));
    })();
  }

  function calibration(version: number, resolved = 0) {
    return {
      strategy_id: MODEL_ID,
      version,
      n_forecasts: resolved ? resolved + 2 : 0,
      n_resolved: resolved,
      brier: resolved ? 0.18 : null,
      brier_base_rate: resolved ? 0.25 : null,
      skill: resolved ? 0.28 : null,
      base_rate: resolved ? 0.5 : null,
      mean_forecast: resolved ? 0.6 : null,
      ece: resolved ? 0.05 : null,
      bins: [],
    };
  }

  /** Answer the calibration reads (roadmap 23.9) that are still open. */
  function flushCalibration(resolved = 0) {
    for (const req of http.match((r) => r.url.endsWith('/calibration'))) {
      const version = Number(req.request.url.split('/').at(-2));
      req.flush(calibration(version, resolved));
    }
  }

  afterEach(() => {
    flushCalibration();
    http.verify();
  });

  it('shows how well each model forecast when it forecasts probabilities', async () => {
    await render([LIVE_V1, CANDIDATE_V2], swapReport(true));
    flushCalibration(20);
    await settle();
    const block = el.querySelector('.calibration')!;
    expect(block.textContent).toContain('Forecast calibration');
    expect(block.querySelectorAll('li').length).toBe(2);
    expect(block.textContent).toContain('20 of 22 resolved');
    expect(block.textContent).toContain('Brier 0.18');
  });

  it('hides calibration for a model without forecasts', async () => {
    await render([LIVE_V1, CANDIDATE_V2], swapReport(true));
    flushCalibration(0);
    await settle();
    expect(el.querySelector('.calibration')).toBeNull();
  });

  it('lists every version and the log, newest first', async () => {
    await render([LIVE_V1]);
    const rows = el.querySelectorAll('app-data-table tbody tr');
    expect(rows.length).toBe(1);
    expect(el.textContent).toContain('Model in use');
    const log = [...el.querySelectorAll('.timeline li')].map((li) => li.textContent);
    expect(log[0]).toContain('New fit saved as a candidate');
    expect(log[1]).toContain('First model recorded');
    expect(el.querySelector('.candidate')).toBeNull();
  });

  it('shows the candidate against the model in use and the passing swap check', async () => {
    await render([LIVE_V1, CANDIDATE_V2], swapReport(true));
    const card = el.querySelector('.candidate')!;
    expect(card.querySelector('h3')!.textContent).toContain('Candidate v2');
    expect(card.textContent).toContain('Over the same 24 days');
    expect(card.textContent).toContain('In use v1');
    expect(card.textContent).toContain('Swap check passed');
    expect(card.querySelectorAll('.checks li').length).toBe(4);
    expect(button('Swap in').disabled).toBe(false);
  });

  it('swaps in a passing candidate after a reason and a fresh code', async () => {
    await render([LIVE_V1, CANDIDATE_V2], swapReport(true));
    const success = vi.spyOn(TestBed.inject(ToastService), 'success');
    button('Swap in').click();
    fixture.detectChanges();
    const form = dialogForm(el)!;
    expect(form.textContent).toContain('Model swap');
    expect(form.textContent).toContain('Candidate v2');
    answerDialog(fixture, { reason: 'better fit' });
    const post = await nextRequest(http, `${BASE}/2/swap`, 'POST');
    expect(ensure).toHaveBeenCalledWith(expect.stringContaining('Swap in model v2'));
    expect(post.request.body).toEqual({ reason: 'better fit', override: false });
    post.flush({ ...CANDIDATE_V2, status: 'live' });
    await reloads([modelArchived(), { ...CANDIDATE_V2, status: 'live' }]);
    await settle();
    expect(success).toHaveBeenCalledWith(
      expect.stringContaining('in use from the next trading run'),
    );
  });

  it('asks for an override with a 20 character reason when the check fails', async () => {
    await render([LIVE_V1, CANDIDATE_V2], swapReport(false));
    expect(el.textContent).toContain('Swap check failed');
    button('Override and swap in…').click();
    fixture.detectChanges();
    expect(fillDialog(fixture, { reason: 'too short' }).disabled).toBe(true);
    answerDialog(fixture, { reason: 'the refit handles the new regime' });
    const post = await nextRequest(http, `${BASE}/2/swap`, 'POST');
    expect(post.request.body).toEqual({
      reason: 'the refit handles the new regime',
      override: true,
    });
    post.flush({ ...CANDIDATE_V2, status: 'live' });
    await reloads([{ ...CANDIDATE_V2, status: 'live' }]);
    await settle();
  });

  it('sends nothing when the code is cancelled', async () => {
    await render([LIVE_V1, CANDIDATE_V2], swapReport(true));
    button('Swap in').click();
    fixture.detectChanges();
    ensure.mockResolvedValue(false);
    answerDialog(fixture, { reason: 'better fit' });
    await settle();
    http.expectNone(`${BASE}/2/swap`);
  });

  it('rejects a candidate with a reason', async () => {
    await render([LIVE_V1, CANDIDATE_V2], swapReport(false));
    button('Reject').click();
    fixture.detectChanges();
    answerDialog(fixture, { reason: 'worse fit' });
    const post = await nextRequest(http, `${BASE}/2/reject`, 'POST');
    expect(post.request.body).toEqual({ reason: 'worse fit' });
    expect(ensure).not.toHaveBeenCalled();
    post.flush({ ...CANDIDATE_V2, status: 'rejected' });
    await reloads([LIVE_V1, { ...CANDIDATE_V2, status: 'rejected' }]);
    await settle();
  });

  it('shows the check to a trader but leaves the swap to admins', async () => {
    await render([LIVE_V1, CANDIDATE_V2], swapReport(true), TRADER);
    expect(button('Swap in').disabled).toBe(true);
    expect(button('Reject').disabled).toBe(true);
    expect(el.querySelector('.candidate app-permission-note')).toBeTruthy();
  });
});

function modelArchived(): ModelVersionView {
  return { ...LIVE_V1, status: 'archived' };
}
