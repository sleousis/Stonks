import { TestBed } from '@angular/core/testing';

import { ResumeSheet } from './resume-sheet';

describe('ResumeSheet', () => {
  function openSheet(live: boolean) {
    const fixture = TestBed.createComponent(ResumeSheet);
    fixture.detectChanges();
    void fixture.componentInstance.open({ lines: [{ label: 'Covers', value: 'Main' }], live });
    fixture.detectChanges();
    const el: HTMLElement = fixture.nativeElement;
    const submit = [...el.querySelectorAll<HTMLButtonElement>('button[type="submit"]')].find((b) =>
      b.textContent?.includes('Resume trading'),
    )!;
    return { el, submit };
  }

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
