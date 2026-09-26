import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import type { StrategyDetail } from '../api/models';
import { provideApi } from '../api/provide-api';
import { SessionService } from '../core/auth/session.service';
import { DraftShip } from '../pages/studio/draft-ship';
import { StrategyDetailPage } from '../pages/strategies/strategy-detail.page';
import { provideFakeChart } from '../../testing/fake-chart';
import { nextRequest, tick } from '../../testing/http';
import { STRATEGY_METADATA } from '../../testing/strategy-fixtures';
import { makeDraft } from '../../testing/studio-fixtures';
import { LIFECYCLE, STAGES, stageOf, stageState } from './governance-labels';

/** Words the console used before UI-11; neither page may show them as actions. */
const OLD_WORDS = /\b(Promote|Enable|Disable|Register|Retire|Move to shadow)\b/;

function buttonTexts(root: HTMLElement, selector = 'button'): string[] {
  return [...root.querySelectorAll(selector)].map((b) => b.textContent?.trim() ?? '');
}

describe('governance labels', () => {
  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [
        ...provideApi(),
        provideHttpClientTesting(),
        provideRouter([]),
        provideFakeChart(),
      ],
    });
    const session = TestBed.inject(SessionService);
    vi.spyOn(session, 'can').mockReturnValue(true);
    vi.spyOn(session, 'whyNot').mockReturnValue(null);
  });

  it('are the agreed words', () => {
    expect(LIFECYCLE.paper.label).toBe('Start paper trading');
    expect(LIFECYCLE.live.label).toBe('Go live');
    expect(LIFECYCLE.stop.label).toBe('Stop');
    expect(LIFECYCLE.paper.target).toBe('shadow');
    expect(LIFECYCLE.live.target).toBe('active');
    expect(LIFECYCLE.stop.target).toBe('retired');
    expect(STAGES.map((s) => s.label)).toEqual(['Draft', 'Paper', 'Ready', 'Live']);
  });

  it('Studio uses them for every lifecycle step', () => {
    const ship = TestBed.createComponent(DraftShip);
    ship.componentRef.setInput('draft', makeDraft());
    ship.detectChanges();
    expect(buttonTexts(ship.nativeElement)).toContain(LIFECYCLE.paper.label);

    ship.componentRef.setInput(
      'draft',
      makeDraft({ status: 'registered', registered_strategy_id: 'x', strategy_status: 'shadow' }),
    );
    ship.detectChanges();
    expect(buttonTexts(ship.nativeElement)).toContain(LIFECYCLE.live.label);

    ship.componentRef.setInput(
      'draft',
      makeDraft({ status: 'registered', registered_strategy_id: 'x', strategy_status: 'active' }),
    );
    ship.detectChanges();
    expect(buttonTexts(ship.nativeElement)).toContain(LIFECYCLE.pause.label);
    expect(buttonTexts(ship.nativeElement).join(' ')).not.toMatch(OLD_WORDS);
  });

  it('Strategies uses the same words', async () => {
    const controller = TestBed.inject(HttpTestingController);
    const page = TestBed.createComponent(StrategyDetailPage);
    page.componentRef.setInput('id', 'mom');
    page.detectChanges();
    const detail: StrategyDetail = {
      id: 'mom',
      status: 'shadow',
      class_path: 'x.MomentumStrategy',
      applicable_asset_classes: ['equity'],
      params: {},
      created_at: '2026-09-01T10:00:00Z',
      updated_at: '2026-09-01T10:00:00Z',
      metadata: STRATEGY_METADATA,
      status_history: [],
      survival_reports: [],
    };
    (await nextRequest(controller, '/api/strategies/mom')).flush(detail);
    (await nextRequest(controller, '/api/strategies/mom/history')).flush([]);
    (await nextRequest(controller, '/api/orders')).flush({
      items: [],
      total: 0,
      limit: 10,
      offset: 0,
    });
    await tick(5);
    page.detectChanges();
    controller.match(() => true); // go-live verdict and paper value are not needed here

    const actions = buttonTexts(page.nativeElement, 'app-page-header button');
    expect(actions).toEqual([LIFECYCLE.live.label, LIFECYCLE.stop.label]);
    expect(actions.join(' ')).not.toMatch(OLD_WORDS);
  });

  it('place a strategy on the stage bar', () => {
    expect(stageOf('draft')).toBe('draft');
    expect(stageOf('shadow', false)).toBe('paper');
    expect(stageOf('shadow', true)).toBe('ready');
    expect(stageOf('active', false)).toBe('live');
    expect(stageState('draft', 'ready')).toBe('done');
    expect(stageState('ready', 'ready')).toBe('current');
    expect(stageState('live', 'ready')).toBe('todo');
  });
});
