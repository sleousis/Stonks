import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { type ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import { nextRequest, tick } from '../../../../testing/http';
import type { ResearchProposalView, ResearchSessionDetailView } from '../../../api/models';
import { provideApi } from '../../../api/provide-api';
import { ResearchSessionPage, outcomeTests } from './research-session.page';

function proposal(seq: number, over: Partial<ResearchProposalView> = {}): ResearchProposalView {
  return {
    id: `rp_${seq}`,
    seq,
    status: 'done',
    class_path: 'stonks.strategies.examples.momentum:Momentum',
    hypothesis: 'Winners keep winning for a few months.',
    premortem: 'Fails in sharp reversals.',
    arguments: { strategy: 'momentum', budget: 20 },
    budget: 20,
    trials: 20,
    cpu_seconds: 120,
    best_score: 0.91,
    verdict: 'fail',
    validation_start: '2025-01-02',
    lab_run_id: `lab_${seq}`,
    outcome: { reports: [{ test_id: 'oos', passed: true, notes: 'Held up.' }] },
    reason: null,
    created_at: '2026-09-25T10:01:00Z',
    finished_at: '2026-09-25T10:05:00Z',
    ...over,
  };
}

const DETAIL: ResearchSessionDetailView = {
  id: 'rs_1',
  goal: 'Find a trend rule on tech',
  status: 'stopped',
  model: 'qwen2.5',
  model_cutoff: '2024-06-01',
  prompt_version: 'research-v1',
  created_at: '2026-09-25T10:00:00Z',
  started_at: '2026-09-25T10:00:05Z',
  finished_at: '2026-09-25T10:30:00Z',
  job_id: 'job_1',
  trials_used: 20,
  max_trials: 200,
  cpu_seconds_used: 120,
  max_cpu_seconds: 3600,
  max_proposals: 10,
  stop_reason: 'The compute budget ran out.',
  summary: 'Momentum failed out of sample.',
  universe: [],
  universe_id: 'sp500',
  proposals: [
    proposal(2, {
      status: 'rejected',
      reason: 'The test window starts before the model cutoff.',
      lab_run_id: null,
      best_score: null,
      verdict: null,
      outcome: null,
      class_path: null,
    }),
    proposal(1),
  ],
};

describe('outcomeTests', () => {
  it('reads survival test lines and ignores odd outcomes', () => {
    expect(outcomeTests(null)).toEqual([]);
    expect(outcomeTests({ reports: 'x' })).toEqual([]);
    expect(outcomeTests({ reports: [{ test_id: 'oos', passed: false }] })[0]).toMatchObject({
      id: 'oos',
      passed: false,
      notes: '',
    });
  });
});

describe('ResearchSessionPage', () => {
  let fixture: ComponentFixture<ResearchSessionPage>;
  let http: HttpTestingController;
  let el: HTMLElement;

  async function settle(): Promise<void> {
    for (let i = 0; i < 3; i++) {
      await tick();
      fixture.detectChanges();
    }
  }

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [...provideApi(), provideHttpClientTesting(), provideRouter([])],
    });
    http = TestBed.inject(HttpTestingController);
    fixture = TestBed.createComponent(ResearchSessionPage);
    fixture.componentRef.setInput('sessionId', 'rs_1');
    el = fixture.nativeElement;
    fixture.detectChanges();
  });

  afterEach(() => http.verify());

  it('shows the budgets used and every proposal in order', async () => {
    (await nextRequest(http, '/api/assistant/research/rs_1')).flush(DETAIL);
    await settle();
    expect(el.querySelector('h2')?.textContent).toContain('Find a trend rule on tech');
    const budgets = el.querySelector('[aria-label="Budget used"]')!.textContent!;
    expect(budgets).toContain('20 of 200');
    expect(budgets).toContain('2 of 10');
    expect(budgets).toContain('2 min of 60 min');
    expect(el.querySelectorAll('meter')).toHaveLength(3);
    expect(el.textContent).toContain('The sp500 universe');
    expect(el.textContent).toContain('The compute budget ran out.');
    expect(el.textContent).toContain('Momentum failed out of sample.');

    const cards = [...el.querySelectorAll('.proposal')];
    expect(cards[0].textContent).toContain('1. Momentum');
    expect(cards[0].textContent).toContain('Winners keep winning');
    expect(cards[0].textContent).toContain('20 of 20');
    expect(cards[0].textContent).toContain('0.91');
    expect(cards[0].textContent).toContain('Held up.');
    expect(cards[0].querySelector('a[href="/lab/ledger/lab_1"]')).not.toBeNull();
    expect(cards[0].querySelector('details')?.textContent).toContain('momentum');

    expect(cards[1].textContent).toContain('The test window starts before the model cutoff.');
    expect(cards[1].textContent).toContain('n/a');
    expect(cards[1].querySelector('a[href^="/lab/ledger/"]')).toBeNull();
    expect(el.querySelector('a.back')?.getAttribute('href')).toBe('/lab/research');
  });

  it('shows a failed load with Retry', async () => {
    (await nextRequest(http, '/api/assistant/research/rs_1')).flush(
      { title: 'x', status: 404, detail: 'no research session' },
      { status: 404, statusText: 'x' },
    );
    await settle();
    expect(el.textContent).toContain('Could not load this research session');
  });
});
