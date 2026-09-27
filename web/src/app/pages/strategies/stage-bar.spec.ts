import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import type { GoLiveReport } from '../../api/models';
import { goLiveReport } from '../../../testing/status-dialog';
import { StageBar } from './stage-bar';

describe('StageBar (UX-64)', () => {
  beforeEach(() => {
    TestBed.configureTestingModule({ providers: [provideRouter([])] });
  });

  function render(
    status: string,
    opts: { passed?: boolean | null; report?: GoLiveReport | null; compact?: boolean } = {},
  ) {
    const fixture = TestBed.createComponent(StageBar);
    fixture.componentRef.setInput('strategyId', 'mom_1a2b3c4d');
    fixture.componentRef.setInput('status', status);
    fixture.componentRef.setInput('golivePassed', opts.passed ?? null);
    fixture.componentRef.setInput('report', opts.report ?? null);
    if (opts.compact) fixture.componentRef.setInput('compact', true);
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  const current = (el: HTMLElement) =>
    el.querySelector('[aria-current="step"] .stage-label')?.textContent?.trim() ?? null;

  it('marks the current stage for each status', () => {
    expect(current(render('draft'))).toBe('Draft');
    expect(current(render('shadow', { passed: false }))).toBe('Paper');
    expect(current(render('shadow', { passed: true }))).toBe('Ready');
    expect(current(render('active'))).toBe('Live');
  });

  it('shows no current step and the way back when stopped', () => {
    const el = render('retired');
    expect(current(el)).toBeNull();
    expect(el.textContent).toContain('Start paper trading');
  });

  it('never offers a Go live button of its own', () => {
    const el = render('shadow', { passed: true });
    expect([...el.querySelectorAll('button')].map((b) => b.textContent?.trim())).not.toContain(
      'Go live',
    );
  });

  it('folds the go-live check under the bar with a fix per failing check (UX-28)', () => {
    const report = goLiveReport('mom_1a2b3c4d', false);
    report.checks.push({
      name: 'promotion_preset',
      passed: false,
      value: 0,
      limit: 1,
      detail: 'no promotion run',
    });
    const el = render('shadow', { passed: false, report });
    const fold = el.querySelector('details.checks')!;
    expect(fold.querySelector('summary')!.textContent).toContain('2 of 3 to fix');
    expect(fold.textContent).toContain('About 17 more trading days on paper.');
    const lab = [...fold.querySelectorAll('a')].find((a) => a.textContent?.includes('Lab'))!;
    expect(lab.getAttribute('href')).toBe('/lab?strategy=mom_1a2b3c4d&preset=promotion');
  });

  it('draws the steps alone when compact', () => {
    const el = render('shadow', { compact: true, report: goLiveReport('mom_1a2b3c4d', false) });
    expect(el.querySelectorAll('.stages li').length).toBe(4);
    expect(el.querySelector('.next')).toBeNull();
    expect(el.querySelector('details')).toBeNull();
    expect(el.querySelector('.panel')).toBeNull();
  });
});
