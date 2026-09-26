import { Component, signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';

import { ApiError } from '../../core/http/api-error';
import { EmptyState, ErrorState, LoadingState } from './states';

@Component({
  imports: [LoadingState, EmptyState, ErrorState],
  template: `
    <app-loading-state label="Loading orders" [rows]="rows()" />
    <app-empty-state title="No orders yet" [message]="message()">
      <a href="/orders/ticks">Try a dry run</a>
    </app-empty-state>
    <app-error-state
      title="Could not load orders"
      [error]="error"
      (retry)="retries = retries + 1"
    />
  `,
})
class Host {
  readonly rows = signal(3);
  readonly message = signal<string | null>('Orders appear after a trading run.');
  error: unknown = new ApiError(500, 'Server error', 'The server failed.');
  retries = 0;
}

describe('shared states', () => {
  function render() {
    const fixture = TestBed.createComponent(Host);
    fixture.detectChanges();
    return { fixture, el: fixture.nativeElement as HTMLElement };
  }

  it('loading: a labelled status region with one bar per row', () => {
    const { fixture, el } = render();
    const loading = el.querySelector('app-loading-state [role="status"]')!;
    expect(loading.getAttribute('aria-label')).toBe('Loading orders');
    expect(loading.querySelectorAll('.bar')).toHaveLength(3);
    fixture.componentInstance.rows.set(6);
    fixture.detectChanges();
    expect(loading.querySelectorAll('.bar')).toHaveLength(6);
  });

  it('empty: title, message and projected action', () => {
    const { fixture, el } = render();
    const empty = el.querySelector('app-empty-state')!;
    expect(empty.querySelector('.title')!.textContent).toContain('No orders yet');
    expect(empty.querySelector('.message')!.textContent).toContain('after a trading run');
    expect(empty.querySelector('a')!.getAttribute('href')).toBe('/orders/ticks');
    fixture.componentInstance.message.set(null);
    fixture.detectChanges();
    expect(empty.querySelector('.message')).toBeNull();
  });

  it('error: an alert with the API message and a retry button', () => {
    const { fixture, el } = render();
    const error = el.querySelector('app-error-state [role="alert"]')!;
    expect(error.textContent).toContain('Could not load orders');
    expect(error.textContent).toContain('The server failed.');
    error.querySelector('button')!.click();
    expect(fixture.componentInstance.retries).toBe(1);
  });
});
