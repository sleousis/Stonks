import { TestBed } from '@angular/core/testing';

import { ToastService } from './toast.service';

describe('ToastService', () => {
  let toasts: ToastService;

  beforeEach(() => {
    vi.useFakeTimers();
    toasts = TestBed.inject(ToastService);
  });

  afterEach(() => vi.useRealTimers());

  const ids = () => toasts.toasts().map((t) => t.id);

  it('dismisses success and info toasts after a few seconds', () => {
    const ok = toasts.success('Saved.');
    const info = toasts.info('Heads up.');
    vi.advanceTimersByTime(5001);
    expect(ids()).toEqual([info]);
    vi.advanceTimersByTime(1000);
    expect(ids()).not.toContain(ok);
    expect(ids()).toEqual([]);
  });

  it('keeps error toasts until they are dismissed', () => {
    const id = toasts.error('The broker refused the order.');
    vi.advanceTimersByTime(10 * 60_000);
    expect(ids()).toEqual([id]);
    toasts.dismiss(id);
    expect(ids()).toEqual([]);
  });

  it('pauses the countdown while focus is inside a toast', () => {
    const id = toasts.success('Saved.');
    vi.advanceTimersByTime(4000);
    toasts.pause(id, 'focus');
    vi.advanceTimersByTime(60_000);
    expect(ids()).toEqual([id]);
    toasts.resume(id, 'focus');
    // The second it had left, not a fresh five.
    vi.advanceTimersByTime(999);
    expect(ids()).toEqual([id]);
    vi.advanceTimersByTime(2);
    expect(ids()).toEqual([]);
  });

  it('waits for both hover and focus to end before counting again', () => {
    const id = toasts.info('Heads up.');
    toasts.pause(id, 'hover');
    toasts.pause(id, 'focus');
    toasts.resume(id, 'hover');
    vi.advanceTimersByTime(60_000);
    expect(ids()).toEqual([id]);
    toasts.resume(id, 'focus');
    vi.advanceTimersByTime(6001);
    expect(ids()).toEqual([]);
  });

  it('ignores pause and resume for errors and unknown ids', () => {
    const id = toasts.error('Failed.');
    toasts.pause(id, 'hover');
    toasts.resume(id, 'hover');
    toasts.pause(999, 'focus');
    vi.advanceTimersByTime(60_000);
    expect(ids()).toEqual([id]);
  });

  it('replaces an identical toast and keeps at most four', () => {
    toasts.error('Same.');
    toasts.error('Same.');
    expect(toasts.toasts().length).toBe(1);
    for (const n of [1, 2, 3, 4]) toasts.success(`Toast ${n}.`);
    expect(toasts.toasts().map((t) => t.message)).toEqual([
      'Toast 1.',
      'Toast 2.',
      'Toast 3.',
      'Toast 4.',
    ]);
  });
});
