import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import type { GoLiveReport } from '../../api/models';
import { checkRow } from '../../shared/golive-checks';
import { GoliveCheckList, sourceWords } from './golive-check-list';

function report(passed: boolean, presetPassed = true): GoLiveReport {
  return {
    strategy_id: 'buyhold-spy',
    status: 'shadow',
    source: 'shadow',
    passed,
    policy: { min_days: 20, max_drawdown: 0.15, max_drift: 0.1, min_trades: 5 },
    checks: [
      { name: 'status', passed: true, value: null, limit: null, detail: 'in shadow' },
      {
        name: 'min_days',
        passed,
        value: passed ? 25 : 3,
        limit: 20,
        detail: passed
          ? '25 days of paper trading, needs at least 20'
          : '3 days of paper trading, needs at least 20',
      },
      {
        name: 'max_drawdown',
        passed: true,
        value: 0.042,
        limit: 0.15,
        detail: 'max drawdown 4.20%, limit 15.00%',
      },
      {
        name: 'max_drift',
        passed: true,
        value: -0.013,
        limit: 0.1,
        detail: 'paper +1.00% vs backtest +2.30% (gap -1.30%, limit ±10.00%)',
      },
      { name: 'min_trades', passed: true, value: 7, limit: 5, detail: '7 trades' },
      { name: 'survival', passed: true, value: 4, limit: 4, detail: '4/4 passed' },
      { name: 'within_mc_band', passed: true, value: 0.042, limit: 0.12, detail: 'inside' },
      { name: 'quit_rule', passed: true, value: 0.042, limit: 0.12, detail: 'within' },
      {
        name: 'promotion_preset',
        passed: presetPassed,
        value: 10,
        limit: 10,
        detail: '10 of 10 tests of the promotion suite on record',
      },
      { name: 'nonzero_costs', passed: true, value: 2, limit: 1, detail: 'fee, spread' },
      { name: 'hypothesis_recorded', passed: true, value: 64, limit: 40, detail: '64 chars' },
      { name: 'backtest_min_trades', passed: true, value: 48, limit: 30, detail: '48 trades' },
    ],
    checklist: {
      n_trials_class: 140,
      dsr: 0.972,
      pbo: null,
      excess_cagr: 0.031,
      min_capital: 4200,
      lot_skipped_share: null,
      hypothesis: 'Recent winners keep winning for a while.',
      premortem: null,
    },
  };
}

describe('GoliveCheckList', () => {
  beforeEach(() => {
    TestBed.configureTestingModule({ providers: [provideRouter([])] });
  });

  function render(r: GoLiveReport): HTMLElement {
    const fixture = TestBed.createComponent(GoliveCheckList);
    fixture.componentRef.setInput('report', r);
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  it('shows only failing checks, each in one sentence, and passing ones as a count (R13)', () => {
    const el = render(report(false));
    // status and the hypothesis length are internal: 10 checks a trader reads
    expect(el.textContent).toContain('9 of 10 checks pass.');
    expect(el.textContent).toContain('Trial result from its own test book.');
    const rows = Array.from(el.querySelectorAll('.checks li'));
    expect(rows.length).toBe(1);
    expect(rows[0].textContent).toContain('Trial days');
    expect(rows[0].querySelector('.check-detail')?.textContent).toContain(
      'It has 3 of the 20 trial days it needs.',
    );
    expect(rows[0].querySelector('.check-fix')?.textContent).toContain(
      'About 17 more trading days on trial.',
    );
    expect(el.textContent).not.toContain('min_days');
    expect(el.textContent).not.toContain('against');
    expect(el.textContent).not.toContain('Hypothesis 64 chars');
    expect(checkRow({ ...report(true).checks[2], value: -0 }).value).toBe('0.00%');
  });

  it('says all checks pass with no list', () => {
    const el = render(report(true));
    expect(el.textContent).toContain('All 10 checks pass.');
    expect(el.querySelectorAll('.checks li').length).toBe(0);
  });

  it('links a failing full-tests row to Lab with the full suite picked (UX-28)', () => {
    const el = render(report(true, false));
    const rows = Array.from(el.querySelectorAll('.checks li'));
    const preset = rows.find((li) => li.textContent?.includes('Full robustness tests'))!;
    expect(preset.querySelector('.check-fix a')?.getAttribute('href')).toBe(
      '/lab?strategy=buyhold-spy&preset=promotion',
    );
  });

  it('shows the checklist a reviewer reads, nulls as n/a, in trader words', () => {
    const el = render(report(true));
    const items = Array.from(el.querySelectorAll('.checklist-grid > div')).map((d) =>
      d.textContent!.replace(/\s+/g, ' ').trim(),
    );
    expect(items).toEqual([
      'Trials of this class 140',
      'Deflated Sharpe 0.972',
      'PBO n/a',
      'Excess CAGR +3.10%',
      'Minimum capital $4.2K',
      'Skipped by whole shares n/a',
      'Hypothesis Recent winners keep winning for a while.',
      'Premortem Not recorded',
    ]);
    expect(el.textContent).toContain('Before you approve');
    expect(el.textContent).not.toMatch(/shadow|promot|regist|go live/i);
  });

  it('names where the trial result came from', () => {
    expect(sourceWords('shadow')).toBe('its own test book');
    expect(sourceWords('portfolio')).toBe('the shared portfolio');
    expect(sourceWords('none')).toContain('no trial record');
  });
});
