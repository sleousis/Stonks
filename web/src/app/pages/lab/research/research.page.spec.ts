import {
  HttpTestingController,
  type TestRequest,
  provideHttpClientTesting,
} from '@angular/common/http/testing';
import { type ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import { TRADER } from '../../../../testing/auth-fixtures';
import { nextRequest, tick } from '../../../../testing/http';
import type { Job, ResearchSessionView } from '../../../api/models';
import { provideApi } from '../../../api/provide-api';
import { SessionService } from '../../../core/auth/session.service';
import { ConfirmService } from '../../../core/confirm/confirm.service';
import { JOB_FETCH, JOB_POLL_MS } from '../../../core/jobs/jobs.service';
import { minutesText, universeText, usedOf } from './research-format';
import {
  ResearchPage,
  buildResearchStart,
  defaultResearchForm,
  researchErrors,
} from './research.page';

export function sessionRow(
  i: number,
  over: Partial<ResearchSessionView> = {},
): ResearchSessionView {
  return {
    id: `rs_${i}`,
    goal: `Find a trend rule ${i}`,
    status: 'done',
    model: 'qwen2.5',
    model_cutoff: '2024-06-01',
    prompt_version: 'research-v1',
    created_at: '2026-09-25T10:00:00Z',
    started_at: '2026-09-25T10:00:05Z',
    finished_at: '2026-09-25T10:30:00Z',
    job_id: `job_${i}`,
    trials_used: 40,
    max_trials: 200,
    cpu_seconds_used: 600,
    max_cpu_seconds: 3600,
    max_proposals: 10,
    stop_reason: null,
    summary: null,
    universe: ['AAA.US', 'BBB.US'],
    universe_id: null,
    ...over,
  };
}

describe('research helpers', () => {
  it('formats budgets and data in plain words', () => {
    expect(minutesText(600)).toBe('10 min');
    expect(minutesText(20)).toBe('under 1 min');
    expect(minutesText(null)).toBe('n/a');
    expect(usedOf(40, 200)).toBe('40 of 200');
    expect(universeText('sp500', [])).toBe('The sp500 universe');
    expect(universeText(null, ['AAA.US'])).toBe('1 ticker: AAA.US');
  });

  it('builds the body with only the budgets filled in', () => {
    const f = { ...defaultResearchForm(), goal: 'A trend rule on tech', tickers: 'aaa.us bbb.us' };
    expect(buildResearchStart(f)).toEqual({
      goal: 'A trend rule on tech',
      universe: ['AAA.US', 'BBB.US'],
    });
    expect(
      buildResearchStart({ ...f, universeId: 'sp500', maxTrials: 50, maxMinutes: 10 }),
    ).toEqual({
      goal: 'A trend rule on tech',
      universe_id: 'sp500',
      max_trials: 50,
      max_cpu_seconds: 600,
    });
    expect(researchErrors(defaultResearchForm())).toMatchObject({
      goal: expect.any(String),
      tickers: expect.any(String),
    });
    expect(researchErrors({ ...f, maxProposals: 0 })['maxProposals']).toBeTruthy();
  });
});

describe('ResearchPage', () => {
  let fixture: ComponentFixture<ResearchPage>;
  let http: HttpTestingController;
  let el: HTMLElement;
  let posted: unknown[];
  let sessions: ResearchSessionView[];
  let startStatus: number;

  function respond(req: TestRequest): void {
    const path = new URL(req.request.urlWithParams, 'http://localhost').pathname;
    if (req.request.method === 'POST' && path === '/api/assistant/research') {
      posted.push(req.request.body);
      if (startStatus !== 202)
        return req.flush(
          { title: 'x', status: startStatus, detail: 'the research loop is off' },
          { status: startStatus, statusText: 'x' },
        );
      return req.flush(job({ status: 'queued', progress: 0 }));
    }
    switch (path) {
      case '/api/assistant/research':
        return req.flush({ items: sessions, total: sessions.length, limit: 25, offset: 0 });
      case '/api/universes':
        return req.flush({
          items: [{ id: 'sp500', name: 'S&P 500', kind: 'index', spec: {} }],
          total: 1,
          limit: 500,
          offset: 0,
        });
      case '/api/jobs/job_new':
        return req.flush(job({}));
    }
    throw new Error(`unexpected ${req.request.method} ${path}`);
  }

  function job(patch: Partial<Job>): Job {
    return {
      id: 'job_new',
      kind: 'assistant_research',
      status: 'succeeded',
      progress: 1,
      created_at: '2026-09-26T10:00:00Z',
      params: { session_id: 'rs_new' },
      ...patch,
    };
  }

  async function settle(rounds = 8): Promise<void> {
    for (let i = 0; i < rounds; i++) {
      http.match(() => true).forEach(respond);
      await tick(5);
      fixture.detectChanges();
    }
  }

  function type(selector: string, value: string): void {
    const node = el.querySelector<HTMLInputElement | HTMLTextAreaElement>(selector)!;
    node.value = value;
    node.dispatchEvent(new Event('input'));
    fixture.detectChanges();
  }

  beforeEach(async () => {
    posted = [];
    sessions = [];
    startStatus = 202;
    TestBed.configureTestingModule({
      providers: [
        ...provideApi(),
        provideHttpClientTesting(),
        provideRouter([]),
        { provide: JOB_FETCH, useValue: () => Promise.reject(new Error('no stream')) },
        { provide: JOB_POLL_MS, useValue: 1 },
      ],
    });
    vi.spyOn(TestBed.inject(ConfirmService), 'confirm').mockResolvedValue(true);
    http = TestBed.inject(HttpTestingController);
    const loading = TestBed.inject(SessionService).load();
    (await nextRequest(http, '/api/auth/me')).flush(TRADER);
    await loading;
  });

  afterEach(() => http.verify());

  async function open(): Promise<void> {
    fixture = TestBed.createComponent(ResearchPage);
    el = fixture.nativeElement;
    fixture.detectChanges();
    await settle();
  }

  it('lists sessions with budgets used and links each one', async () => {
    sessions = [sessionRow(1), sessionRow(2, { status: 'running', finished_at: null })];
    await open();
    expect(el.textContent).toContain('2 sessions');
    expect(el.textContent).toContain('Find a trend rule 1');
    expect(el.textContent).toContain('40 of 200');
    expect(el.textContent).toContain('10 min of 60 min');
    expect(el.querySelector('a[href="/lab/research/rs_1"]')).not.toBeNull();
    expect(el.querySelector('nav[aria-label="Lab screens"]')).not.toBeNull();
  });

  it('explains how sessions start when there are none', async () => {
    await open();
    expect(el.textContent).toContain('No research sessions yet');
    expect(el.textContent).toContain('ask the assistant in the chat');
  });

  it('checks the form, posts the goal and universe, then links the session', async () => {
    await open();
    el.querySelector<HTMLButtonElement>('button[type="submit"]')!.click();
    await settle(2);
    expect(posted).toHaveLength(0);
    expect(el.textContent).toContain('Say what to look for');

    type('#rs-goal', 'A trend rule that holds up after costs');
    const select = el.querySelector<HTMLSelectElement>('#rs-universe')!;
    select.value = 'sp500';
    select.dispatchEvent(new Event('change'));
    fixture.detectChanges();
    type('#rs-trials', '30');
    el.querySelector<HTMLButtonElement>('button[type="submit"]')!.click();
    await settle();
    expect(posted[0]).toEqual({
      goal: 'A trend rule that holds up after costs',
      universe_id: 'sp500',
      max_trials: 30,
    });
    expect(el.querySelector('a[href="/lab/research/rs_new"]')).not.toBeNull();
  });

  it('says what is missing when the research loop is off', async () => {
    startStatus = 503;
    await open();
    type('#rs-goal', 'A trend rule that holds up after costs');
    type('#rs-tickers', 'aaa.us');
    el.querySelector<HTMLButtonElement>('button[type="submit"]')!.click();
    await settle();
    expect(posted[0]).toMatchObject({ universe: ['AAA.US'] });
    expect(el.textContent).toContain('training cutoff date');
  });
});
