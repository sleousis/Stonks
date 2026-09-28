import { TestBed } from '@angular/core/testing';

import { StatusPill } from './status-pill';

describe('StatusPill', () => {
  function render(status: string | null, label: string | null = null) {
    const fixture = TestBed.createComponent(StatusPill);
    fixture.componentRef.setInput('status', status);
    fixture.componentRef.setInput('label', label);
    fixture.detectChanges();
    return (fixture.nativeElement as HTMLElement).textContent!.trim();
  }

  it('says On trial, Approved and Retired for strategy statuses, never Live (B1)', () => {
    expect(render('shadow')).toBe('On trial');
    expect(render('active')).toBe('Approved');
    expect(render('retired')).toBe('Retired');
  });

  it('writes out outcomes as Passed and Failed', () => {
    expect(render('pass')).toBe('Passed');
    expect(render('passed')).toBe('Passed');
    expect(render('fail')).toBe('Failed');
    expect(render('FAILED')).toBe('Failed');
  });

  it('keeps an explicit label and other statuses as given', () => {
    expect(render('active', 'Turned on')).toBe('Turned on');
    expect(render('filled')).toBe('filled');
  });
});
