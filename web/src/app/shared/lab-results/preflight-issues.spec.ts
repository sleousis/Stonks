import { TestBed } from '@angular/core/testing';

import type { PreflightView } from '../../api/models';
import { PreflightIssues, issueRows } from './preflight-issues';

describe('issueRows', () => {
  it('puts errors first and turns details into short lines', () => {
    const rows = issueRows({
      ok: false,
      skipped: false,
      issues: [
        { code: 'stale_bars', severity: 'warning', message: 'Old bars.', details: { days: 9 } },
        {
          code: 'flagged_statements',
          severity: 'error',
          message: 'Flags.',
          details: { tickers: Array.from({ length: 10 }, (_, i) => `T${i}`) },
        },
      ],
    });
    expect(rows.map((r) => r.severity)).toEqual(['error', 'warning']);
    expect(rows[0].label).toBe('Flagged statements');
    expect(rows[0].details).toEqual(['Tickers: T0, T1, T2, T3, T4, T5, T6, T7 and 2 more']);
    expect(rows[1].details).toEqual(['Days: 9']);
    expect(issueRows(null)).toEqual([]);
  });
});

describe('PreflightIssues', () => {
  function render(preflight: PreflightView) {
    const fixture = TestBed.createComponent(PreflightIssues);
    fixture.componentRef.setInput('preflight', preflight);
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  it('says when checks were skipped or passed clean', () => {
    expect(render({ ok: true, skipped: true, issues: [] }).textContent).toContain('skipped');
    expect(render({ ok: true, skipped: false, issues: [] }).textContent).toContain(
      'passed with no warnings',
    );
  });
});
