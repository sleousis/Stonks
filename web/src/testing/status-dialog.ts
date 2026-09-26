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
  return form.querySelector<HTMLButtonElement>('button[type="submit"]')!;
}

/** Fill and press the dialog's confirm button. */
export function answerDialog(
  fixture: ComponentFixture<unknown>,
  answer: { reason: string; typed?: string },
): void {
  const submit = fillDialog(fixture, answer);
  if (submit.disabled) throw new Error('confirm button is disabled');
  submit.click();
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
