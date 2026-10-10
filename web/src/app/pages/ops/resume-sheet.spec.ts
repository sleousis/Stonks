import { TestBed } from '@angular/core/testing';

import { PAPER_RESUME_REASON, ResumeSheet } from './resume-sheet';

describe('ResumeSheet', () => {
  function openSheet(live: boolean) {
    const fixture = TestBed.createComponent(ResumeSheet);
    fixture.detectChanges();
    const answer = fixture.componentInstance.open({
      lines: [{ label: 'Covers', value: 'Main' }],
      live,
    });
    fixture.detectChanges();
    const el: HTMLElement = fixture.nativeElement;
    const submit = [...el.querySelectorAll<HTMLButtonElement>('button[type="submit"]')].find((b) =>
      b.textContent?.includes('Resume trading'),
    )!;
    return { el, submit, answer, fixture };
  }

  it('resumes paper in one tap: no phrase, no reason box, no checks (PO-07)', async () => {
    const { el, submit, answer } = openSheet(false);
    expect(el.querySelector('#resume-typed')).toBeNull();
    expect(el.querySelector('#resume-reason')).toBeNull();
    expect(el.querySelector('.checks')).toBeNull();
    expect(submit.disabled).toBe(false);
    submit.click();
    expect(await answer).toEqual({ reason: PAPER_RESUME_REASON, overrideChecks: false });
  });

  it('still shows a failed check on paper, behind the override', () => {
    const { el, fixture } = openSheet(false);
    fixture.componentInstance.setChecks({
      halt_id: 1,
      passed: false,
      checks: [{ name: 'gateway_up', passed: false, detail: 'down', portfolio_id: null }],
    });
    fixture.detectChanges();
    expect(el.querySelector('.checks')?.textContent).toContain('Gateway up');
    expect(el.querySelector('#resume-override')).not.toBeNull();
  });

  it('keeps the reason, the typed words and the checks for real money', () => {
    const { el, submit } = openSheet(true);
    expect(el.querySelector('#resume-typed')).not.toBeNull();
    expect(el.querySelector('#resume-reason')).not.toBeNull();
    expect(el.querySelector('.checks')).not.toBeNull();
    expect(submit.disabled).toBe(true);
  });

  it('uses the primary button for a paper portfolio: red is for real money only', () => {
    const { el, submit } = openSheet(false);
    expect(submit.classList).toContain('btn-primary');
    expect(submit.classList).not.toContain('btn-danger');
    expect(el.querySelector('app-mode-stamp')?.textContent).toContain('PAPER');
  });

  it('keeps the red button at a real money stage', () => {
    const { submit } = openSheet(true);
    expect(submit.classList).toContain('btn-danger');
    expect(submit.classList).not.toContain('btn-primary');
  });
});
