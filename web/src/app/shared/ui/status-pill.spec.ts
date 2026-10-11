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

  it('writes job and order statuses in capitalised words', () => {
    expect(render('succeeded')).toBe('Done');
    expect(render('filled')).toBe('Filled');
    expect(render('running')).toBe('Running');
    expect(render('queued')).toBe('Waiting');
    expect(render('cancelled')).toBe('Cancelled');
  });

  it('keeps an explicit label and other statuses as given', () => {
    expect(render('active', 'Turned on')).toBe('Turned on');
    expect(render('stale')).toBe('stale');
  });

  it('gives not-yet states quiet neutral receipts, never red', () => {
    expect(render('to_do')).toBe('To do');
    expect(render('not_yet')).toBe('Not yet');
    expect(render('optional')).toBe('Optional');
    expect(render('waiting_admin')).toBe('Waiting for an admin');
    expect(render('done')).toBe('Done');
    const fixture = TestBed.createComponent(StatusPill);
    fixture.componentRef.setInput('status', 'not_yet');
    fixture.detectChanges();
    const el = fixture.nativeElement as HTMLElement;
    expect(el.getAttribute('data-tone')).toBe('neutral');
    expect(el.getAttribute('data-form')).toBe('receipt');
  });
});
