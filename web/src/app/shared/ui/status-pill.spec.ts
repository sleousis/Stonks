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

  it('says Paper trading, Live and Stopped for strategy statuses (UX-09)', () => {
    expect(render('shadow')).toBe('Paper trading');
    expect(render('active')).toBe('Live');
    expect(render('retired')).toBe('Stopped');
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
