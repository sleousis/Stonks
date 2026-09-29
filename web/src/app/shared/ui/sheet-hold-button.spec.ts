import { TestBed } from '@angular/core/testing';

import { ARM_MS, HOLD_MS, HoldButton } from './sheet-hold-button';

describe('HoldButton', () => {
  function setup(disabled = false) {
    const fixture = TestBed.createComponent(HoldButton);
    fixture.componentRef.setInput('label', 'Go live');
    fixture.componentRef.setInput('disabled', disabled);
    const confirmed = vi.fn();
    fixture.componentInstance.confirmed.subscribe(confirmed);
    fixture.detectChanges();
    const button = (fixture.nativeElement as HTMLElement).querySelector('button')!;
    return { fixture, button, confirmed };
  }

  function key(button: HTMLButtonElement, type: 'keydown' | 'keyup', k: string, repeat = false) {
    const e = new KeyboardEvent(type, { key: k, repeat, bubbles: true, cancelable: true });
    button.dispatchEvent(e);
    return e;
  }

  function pointer(button: HTMLButtonElement, type: string) {
    const e = new MouseEvent(type, { bubbles: true, button: 0 });
    button.dispatchEvent(e);
  }

  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it('names the action and explains how to use it', () => {
    const { button } = setup();
    expect(button.textContent).toContain('Hold to go live');
    const hint = document.getElementById(button.getAttribute('aria-describedby')!);
    expect(hint?.textContent).toContain('press twice');
  });

  it('confirms after a full press and hold', () => {
    const { fixture, button, confirmed } = setup();
    pointer(button, 'pointerdown');
    fixture.detectChanges();
    expect(button.textContent).toContain('Keep holding');
    vi.advanceTimersByTime(HOLD_MS - 1);
    expect(confirmed).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1);
    expect(confirmed).toHaveBeenCalledTimes(1);
  });

  it('cancels when let go early', () => {
    const { fixture, button, confirmed } = setup();
    pointer(button, 'pointerdown');
    vi.advanceTimersByTime(HOLD_MS / 2);
    pointer(button, 'pointerup');
    vi.advanceTimersByTime(HOLD_MS);
    fixture.detectChanges();
    expect(confirmed).not.toHaveBeenCalled();
    expect(button.textContent).toContain('Hold a little longer');
  });

  it('works by holding Enter or Space, without a synthetic click', () => {
    const { button, confirmed } = setup();
    const down = key(button, 'keydown', 'Enter');
    expect(down.defaultPrevented).toBe(true);
    key(button, 'keydown', 'Enter', true); // key repeat does not restart the timer
    vi.advanceTimersByTime(HOLD_MS);
    expect(confirmed).toHaveBeenCalledTimes(1);

    key(button, 'keyup', 'Enter');
    key(button, 'keydown', ' ');
    vi.advanceTimersByTime(HOLD_MS / 2);
    const up = key(button, 'keyup', ' ');
    expect(up.defaultPrevented).toBe(true);
    vi.advanceTimersByTime(HOLD_MS);
    expect(confirmed).toHaveBeenCalledTimes(1);
  });

  it('lets a screen reader confirm with two presses', () => {
    const { fixture, button, confirmed } = setup();
    button.click(); // detail 0: no pointer, no key
    fixture.detectChanges();
    expect(button.textContent).toContain('Press again to confirm');
    // Announced: a screen reader does not read a label change on the focused button.
    const live = (fixture.nativeElement as HTMLElement).querySelector('[aria-live="polite"]');
    expect(live?.textContent).toContain('Press again to confirm');
    expect(confirmed).not.toHaveBeenCalled();
    button.click();
    expect(confirmed).toHaveBeenCalledTimes(1);
  });

  it('disarms the screen reader fallback after a pause', () => {
    const { fixture, button, confirmed } = setup();
    button.click();
    vi.advanceTimersByTime(ARM_MS);
    fixture.detectChanges();
    expect(button.textContent).toContain('Hold to go live');
    button.click();
    expect(confirmed).not.toHaveBeenCalled();
  });

  it('ignores mouse clicks that were not held', () => {
    const { button, confirmed } = setup();
    button.dispatchEvent(new MouseEvent('click', { bubbles: true, detail: 1 }));
    button.dispatchEvent(new MouseEvent('click', { bubbles: true, detail: 1 }));
    expect(confirmed).not.toHaveBeenCalled();
  });

  it('does nothing while disabled', () => {
    const { button, confirmed } = setup(true);
    expect(button.disabled).toBe(true);
    pointer(button, 'pointerdown');
    key(button, 'keydown', 'Enter');
    vi.advanceTimersByTime(HOLD_MS);
    expect(confirmed).not.toHaveBeenCalled();
  });
});
