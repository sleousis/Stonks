import { type ComponentFixture, TestBed } from '@angular/core/testing';

import { ToastService } from '../../core/notify/toast.service';
import { ToastOutlet } from './toast-outlet';

describe('ToastOutlet', () => {
  let fixture: ComponentFixture<ToastOutlet>;
  let el: HTMLElement;
  let toasts: ToastService;
  let show: ReturnType<typeof vi.fn>;
  let hide: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    vi.useFakeTimers();
    // jsdom has no popover API: stand in for it.
    show = vi.fn();
    hide = vi.fn();
    Object.assign(HTMLElement.prototype, { showPopover: show, hidePopover: hide });
    fixture = TestBed.createComponent(ToastOutlet);
    el = fixture.nativeElement;
    toasts = TestBed.inject(ToastService);
    fixture.detectChanges();
  });

  afterEach(() => {
    vi.useRealTimers();
    delete (HTMLElement.prototype as Partial<HTMLElement>).showPopover;
    delete (HTMLElement.prototype as Partial<HTMLElement>).hidePopover;
  });

  const layer = () => el.querySelector<HTMLElement>('.toasts')!;
  const items = () => [...el.querySelectorAll<HTMLElement>('.toast')];

  it('renders in a manual popover without a second live region', () => {
    expect(layer().getAttribute('popover')).toBe('manual');
    expect(layer().hasAttribute('aria-live')).toBe(false);
    expect(show).not.toHaveBeenCalled();
  });

  it('names its region apart from the Notifications panel (A11Y-1)', () => {
    expect(layer().getAttribute('aria-label')).toBe('Messages');
  });

  it('gives errors role alert and other toasts role status', () => {
    toasts.error('Could not reach the broker.', 'Order failed');
    toasts.success('Saved.');
    fixture.detectChanges();
    expect(items().map((t) => t.getAttribute('role'))).toEqual(['alert', 'status']);
    expect(items()[0].textContent).toContain('Order failed');
    expect(items()[0].textContent).toContain('Could not reach the broker.');
  });

  it('opens the top layer while toasts are showing and closes it after', () => {
    const id = toasts.error('Failed.');
    fixture.detectChanges();
    expect(show).toHaveBeenCalledTimes(1);

    // A new toast raises the layer again, above any dialog opened since.
    toasts.info('Another.');
    fixture.detectChanges();
    expect(hide).toHaveBeenCalledTimes(1);
    expect(show).toHaveBeenCalledTimes(2);

    toasts.dismiss(id);
    vi.advanceTimersByTime(7000);
    fixture.detectChanges();
    expect(items()).toHaveLength(0);
    expect(hide).toHaveBeenCalledTimes(2);
  });

  it('dismisses a toast from its close button', () => {
    toasts.error('Failed.');
    fixture.detectChanges();
    el.querySelector<HTMLButtonElement>('button[aria-label="Dismiss notification"]')!.click();
    fixture.detectChanges();
    expect(items()).toHaveLength(0);
  });

  it('pauses the countdown on hover and on focus', () => {
    toasts.success('Saved.');
    fixture.detectChanges();
    const toast = items()[0];

    toast.dispatchEvent(new MouseEvent('mouseenter'));
    vi.advanceTimersByTime(30_000);
    fixture.detectChanges();
    expect(items()).toHaveLength(1);
    toast.dispatchEvent(new MouseEvent('mouseleave'));

    toast.querySelector('button')!.dispatchEvent(new FocusEvent('focusin', { bubbles: true }));
    vi.advanceTimersByTime(30_000);
    fixture.detectChanges();
    expect(items()).toHaveLength(1);

    toast
      .querySelector('button')!
      .dispatchEvent(new FocusEvent('focusout', { bubbles: true, relatedTarget: null }));
    vi.advanceTimersByTime(5001);
    fixture.detectChanges();
    expect(items()).toHaveLength(0);
  });
});
