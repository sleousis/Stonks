import type { ComponentFixture } from '@angular/core/testing';

import type { GoLiveReport } from '../app/api/models';

/** The open <app-status-change-dialog> form, or null. */
export function dialogForm(root: HTMLElement): HTMLFormElement | null {
  return root.querySelector('app-status-change-dialog form');
}

function type(el: HTMLInputElement | HTMLTextAreaElement | null, text: string): void {
  if (!el) throw new Error('field not found');
  el.value = text;
  el.dispatchEvent(new Event('input'));
}

/**
 * The dialog's confirm control: the submit button, or the hold-to-confirm
 * button when the dialog asks for a hold instead of typing.
 */
export function confirmButton(form: HTMLFormElement): HTMLButtonElement {
  const button =
    form.querySelector<HTMLButtonElement>('button[type="submit"]') ??
    form.querySelector<HTMLButtonElement>('app-hold-button button');
  if (!button) throw new Error('no confirm button');
  return button;
}

/** True when the open dialog confirms by holding rather than typing. */
export function isHoldDialog(root: HTMLElement): boolean {
  return !!dialogForm(root)?.querySelector('app-hold-button');
}

/** Fill the reason (and the typed confirmation when asked) without submitting. */
export function fillDialog(
  fixture: ComponentFixture<unknown>,
  answer: { reason: string; typed?: string },
): HTMLButtonElement {
  const form = dialogForm(fixture.nativeElement as HTMLElement);
  if (!form) throw new Error('no status dialog open');
  type(form.querySelector('textarea'), answer.reason);
  if (answer.typed !== undefined) type(form.querySelector('input'), answer.typed);
  fixture.detectChanges();
  return confirmButton(form);
}

/**
 * Fill and confirm. A hold dialog is confirmed through its screen reader
 * fallback (two presses with no pointer), which needs no fake timers.
 */
export function answerDialog(
  fixture: ComponentFixture<unknown>,
  answer: { reason: string; typed?: string },
): void {
  const hold = isHoldDialog(fixture.nativeElement as HTMLElement);
  const button = fillDialog(fixture, answer);
  if (button.disabled) throw new Error('confirm button is disabled');
  button.click();
  if (hold) {
    fixture.detectChanges();
    button.click();
  }
  fixture.detectChanges();
}

export function goLiveReport(id: string, passed: boolean): GoLiveReport {
  return {
    strategy_id: id,
    status: 'shadow',
    source: 'shadow',
    passed,
    policy: { min_days: 20, max_drawdown: 0.15, max_drift: 0.1, min_trades: 5 },
    checks: [
      { name: 'status', passed: true, value: null, limit: null, detail: 'shadow' },
      {
        name: 'min_days',
        passed,
        value: passed ? 25 : 3,
        limit: 20,
        detail: passed ? '25 paper day(s), need >= 20' : '3 paper day(s), need >= 20',
      },
    ],
  };
}
