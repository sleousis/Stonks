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

  it('uses the strategy ladder words, never Live (B1)', () => {
    const el = render('active');
    const labels = [...el.querySelectorAll('.stage-label')].map((s) => s.textContent?.trim());
    expect(labels).toEqual(['Draft', 'On trial', 'Approved', 'Retired']);
    expect(el.textContent).not.toMatch(/(?<!go-)\blive\b/i);
  });

  it('marks the current step for each status', () => {
    expect(current(render('draft'))).toBe('Draft');
    expect(current(render('shadow', { passed: false }))).toBe('On trial');
    expect(current(render('active'))).toBe('Approved');
    expect(current(render('retired'))).toBe('Retired');
  });

  it('says a strategy on trial that passed the check is ready to approve', () => {
    const el = render('shadow', { passed: true });
    expect(current(el)).toBe('On trial');
    expect(el.textContent).toContain('ready to approve');
    const link = el.querySelector<HTMLAnchorElement>('.next a')!;
    expect(link.getAttribute('href')).toBe('/strategies/mom_1a2b3c4d?tab=review');
  });

  it('lights only the retired step and shows the way back', () => {
    const el = render('retired');
    expect(el.querySelectorAll('[data-state="done"]').length).toBe(0);
    expect(el.textContent).toContain('Put on trial');
  });

  it('never offers an Approve button of its own', () => {
    const el = render('shadow', { passed: true });
    expect([...el.querySelectorAll('button')].map((b) => b.textContent?.trim())).not.toContain(
      'Approve',
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
    expect(fold.textContent).toContain('About 17 more trading days on trial.');
    expect(fold.textContent).not.toMatch(/promotion/i);
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
