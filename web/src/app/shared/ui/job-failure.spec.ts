import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';

import { SessionService } from '../../core/auth/session.service';
import { JobFailure } from './job-failure';

function render(admin: boolean, detail: string | null) {
  TestBed.configureTestingModule({
    providers: [{ provide: SessionService, useValue: { isAdmin: signal(admin) } }],
  });
  const fixture = TestBed.createComponent(JobFailure);
  fixture.componentRef.setInput('text', 'The job stopped because of a problem on our side.');
  fixture.componentRef.setInput('detail', detail);
  fixture.detectChanges();
  return fixture.nativeElement as HTMLElement;
}

describe('JobFailure', () => {
  it('shows the plain message as an alert', () => {
    const el = render(false, null);
    expect(el.querySelector('[role="alert"]')!.textContent).toContain(
      'The job stopped because of a problem on our side.',
    );
    expect(el.querySelector('details')).toBeNull();
  });

  it('an admin sees the raw error under Details', () => {
    const el = render(true, "TypeError: cannot pickle '_thread.lock' object");
    expect(el.querySelector('details summary')!.textContent!.trim()).toBe('Details');
    expect(el.querySelector('details code')!.textContent).toContain('cannot pickle');
  });

  it('a trader never sees the raw error, even when one is passed', () => {
    const el = render(false, "TypeError: cannot pickle '_thread.lock' object");
    expect(el.querySelector('details')).toBeNull();
    expect(el.textContent).not.toContain('pickle');
  });

  it('an admin sees no Details when there is no raw error', () => {
    const el = render(true, '   ');
    expect(el.querySelector('details')).toBeNull();
  });
});
