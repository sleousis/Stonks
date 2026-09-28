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
import {
  LIFECYCLE,
  MODES,
  STAGES,
  STATUS_WORDS,
  readyToApprove,
  stageOf,
  stageState,
  toTraderWords,
} from './governance-labels';

/**
 * Words the console used before (UI-11, then the vocabulary of 2026-09-28);
 * neither page may show them as actions.
 */
const OLD_WORDS = /\b(Promote|Enable|Disable|Register|Go live|Stop|Move to shadow|paper trading)\b/;

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
    // docs/design/vocabulary.md: three ladders, no shared words.
    expect(LIFECYCLE.paper.label).toBe('Put on trial');
    expect(LIFECYCLE.live.label).toBe('Approve');
    expect(LIFECYCLE.pause.label).toBe('Back on trial');
    expect(LIFECYCLE.stop.label).toBe('Retire');
    expect(LIFECYCLE.paper.target).toBe('shadow');
    expect(LIFECYCLE.live.target).toBe('active');
    expect(LIFECYCLE.stop.target).toBe('retired');
    expect(STAGES.map((s) => s.label)).toEqual(['Draft', 'On trial', 'Approved', 'Retired']);
    expect(STATUS_WORDS).toEqual({ shadow: 'On trial', active: 'Approved', retired: 'Retired' });
    expect(MODES.map((m) => m.label)).toEqual([
      'Alerts only',
      'Paper',
      'Approve each trade',
      'Automatic',
    ]);
    expect(LIFECYCLE.live.done('X')).toBe('X is approved. People can follow it.');
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
    controller.match(() => true); // the trial record is not needed here

    const actions = buttonTexts(page.nativeElement, 'app-page-header button');
    // Not ready yet: no Approve, the admin's override instead (UX-23). Retire
    // is never in the header (M9).
    expect(actions).toEqual(['Override…']);
    expect(actions.join(' ')).not.toMatch(OLD_WORDS);
  });

  it('place a strategy on its status ladder', () => {
    expect(stageOf('draft')).toBe('draft');
    expect(stageOf('shadow')).toBe('trial');
    expect(stageOf('active')).toBe('approved');
    expect(stageOf('retired')).toBe('retired');
    expect(readyToApprove('shadow', true)).toBe(true);
    expect(readyToApprove('shadow', false)).toBe(false);
    expect(readyToApprove('active', true)).toBe(false);
    expect(stageState('draft', 'trial')).toBe('done');
    expect(stageState('trial', 'trial')).toBe('current');
    expect(stageState('approved', 'trial')).toBe('todo');
  });

  it('put server lines in the words people read', () => {
    expect(toTraderWords('strategy is in shadow, not active')).toBe(
      'strategy is on trial, not approved yet',
    );
    expect(toTraderWords('3 days of paper trading on the model book')).toBe(
      '3 days on trial on the test book',
    );
    expect(toTraderWords("run the 'promotion' preset")).toBe('run the full robustness tests');
    expect(toTraderWords('Paper trading, measured on its paper trading results')).toBe(
      'On trial, measured on its trial results',
    );
    expect(toTraderWords('0 paper trades filled')).toBe('0 trial trades filled');
  });
});
