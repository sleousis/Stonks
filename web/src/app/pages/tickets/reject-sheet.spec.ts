import { TestBed } from '@angular/core/testing';

import { RejectSheet } from './reject-sheet';

describe('RejectSheet', () => {
  function submitButton(sheet: HTMLElement): HTMLButtonElement {
    return sheet.querySelector<HTMLButtonElement>('button[type="submit"]')!;
  }

  it('shows a red Reject only for a real-money ticket', () => {
    const fixture = TestBed.createComponent(RejectSheet);
    fixture.detectChanges();
    const el = fixture.nativeElement as HTMLElement;
    void fixture.componentInstance.open('Buy 5 AAA.US', false);
    fixture.detectChanges();
    expect(submitButton(el).classList).not.toContain('btn-danger');
    void fixture.componentInstance.open('Buy 5 AAA.US', true);
    fixture.detectChanges();
    expect(submitButton(el).classList).toContain('btn-danger');
  });
});
