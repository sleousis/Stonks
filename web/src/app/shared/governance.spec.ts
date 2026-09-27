import { ChangeDetectionStrategy, Component, viewChild } from '@angular/core';
import { type ComponentFixture, TestBed } from '@angular/core/testing';

import type { GoLiveReport, StatusChangeRequest } from '../api/models';
import { ApiError } from '../core/http/api-error';
import type { ToastService } from '../core/notify/toast.service';
import { tick } from '../../testing/http';
import {
  answerDialog,
  dialogForm,
  fillDialog,
  goLiveReport,
  isHoldDialog,
} from '../../testing/status-dialog';
import { isGoLiveRefusal, promoteThroughGate } from './governance';
import { StatusChangeDialog } from './ui/status-change-dialog';

@Component({
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [StatusChangeDialog],
  template: `<app-status-change-dialog />`,
})
class Host {
  readonly dialog = viewChild.required(StatusChangeDialog);
}

function refusal(): ApiError {
  return new ApiError(409, 'Conflict', 'Go-live gate failed.');
}

describe('promoteThroughGate', () => {
  let fixture: ComponentFixture<Host>;
  let el: HTMLElement;
  let toasts: { error: ReturnType<typeof vi.fn> };
  let promote: ReturnType<typeof vi.fn<(body: StatusChangeRequest) => Promise<string>>>;
  let golive: ReturnType<typeof vi.fn<() => Promise<GoLiveReport>>>;
  let busy: boolean[];

  beforeEach(async () => {
    fixture = TestBed.createComponent(Host);
    await fixture.whenStable();
    el = fixture.nativeElement;
    toasts = { error: vi.fn() };
    promote = vi.fn<(body: StatusChangeRequest) => Promise<string>>();
    golive = vi.fn<() => Promise<GoLiveReport>>().mockResolvedValue(goLiveReport('mom', true));
    busy = [];
  });

  function start() {
    return promoteThroughGate({
      id: 'mom',
      dialog: fixture.componentInstance.dialog(),
      toasts: toasts as unknown as ToastService,
      golive,
      promote,
      title: 'Go live with mom?',
      message: 'It trades from the next run.',
      confirmLabel: 'Go live',
      busy: (on) => busy.push(on),
    });
  }

  async function settle(): Promise<void> {
    await tick();
    fixture.detectChanges();
  }

  function cancel(): void {
    const button = [...dialogForm(el)!.querySelectorAll('button')].find(
      (b) => b.textContent?.trim() === 'Cancel',
    )!;
    button.click();
  }

  it('shows the go-live verdict and confirms a passing promotion by holding, not typing', async () => {
    promote.mockResolvedValue('done');
    const result = start();
    await settle();

    expect(dialogForm(el)!.textContent).toContain('Go-live check passed');
    expect(isHoldDialog(el)).toBe(true);
    expect(dialogForm(el)!.querySelector('input')).toBeNull();

    answerDialog(fixture, { reason: 'Paper period looks solid' });
    await expect(result).resolves.toBe('done');
    expect(promote).toHaveBeenCalledWith({ reason: 'Paper period looks solid', override: false });
    expect(busy).toEqual([true, false, true, false]);
  });

  it('resolves null and never promotes when the first dialog is cancelled', async () => {
    const result = start();
    await settle();
    cancel();
    await expect(result).resolves.toBeNull();
    expect(promote).not.toHaveBeenCalled();
  });

  it('still asks when the go-live report fails to load, with a note', async () => {
    golive.mockRejectedValue(new ApiError(500, 'x', 'boom'));
    promote.mockResolvedValue('done');
    const result = start();
    await settle();
    expect(dialogForm(el)!.textContent).toContain('Could not run the go-live check first (boom)');
    answerDialog(fixture, { reason: 'why' });
    await expect(result).resolves.toBe('done');
  });

  it('on a 409 opens the override dialog and retries with override true', async () => {
    promote.mockRejectedValueOnce(refusal()).mockResolvedValueOnce('overridden');
    const result = start();
    await settle();
    answerDialog(fixture, { reason: 'first try' });
    await settle();

    const form = dialogForm(el)!;
    expect(form.textContent).toContain('The go-live gate refused mom');
    expect(form.textContent).toContain('Go-live gate failed.');
    expect(isHoldDialog(el)).toBe(false);
    expect(form.textContent).toContain('Type override to confirm');

    // Too short for an override.
    expect(fillDialog(fixture, { reason: 'Short reason', typed: 'override' }).disabled).toBe(true);

    answerDialog(fixture, { reason: 'Board approved the early start', typed: 'override' });
    await expect(result).resolves.toBe('overridden');
    expect(promote).toHaveBeenLastCalledWith({
      reason: 'Board approved the early start',
      override: true,
    });
    expect(toasts.error).not.toHaveBeenCalled();
  });

  it('resolves null when the override dialog is cancelled', async () => {
    promote.mockRejectedValueOnce(refusal());
    const result = start();
    await settle();
    answerDialog(fixture, { reason: 'first try' });
    await settle();
    cancel();
    await expect(result).resolves.toBeNull();
    expect(promote).toHaveBeenCalledTimes(1);
  });

  it('toasts other errors and stops', async () => {
    promote.mockRejectedValue(new ApiError(500, 'Server error', 'down'));
    const result = start();
    await settle();
    answerDialog(fixture, { reason: 'go' });
    await expect(result).resolves.toBeNull();
    expect(toasts.error).toHaveBeenCalledWith('down', 'Server error');
    expect(dialogForm(el)).toBeNull();
  });

  it('toasts an error from the override retry', async () => {
    promote
      .mockRejectedValueOnce(refusal())
      .mockRejectedValueOnce(new ApiError(500, 'Oops', 'again'));
    const result = start();
    await settle();
    answerDialog(fixture, { reason: 'first try' });
    await settle();
    answerDialog(fixture, { reason: 'Board approved the early start', typed: 'override' });
    await expect(result).resolves.toBeNull();
    expect(toasts.error).toHaveBeenCalledWith('again', 'Oops');
  });
});

describe('isGoLiveRefusal', () => {
  it('is true only for a 409 ApiError', () => {
    expect(isGoLiveRefusal(refusal())).toBe(true);
    expect(isGoLiveRefusal(new ApiError(400, 'x', 'y'))).toBe(false);
    expect(isGoLiveRefusal(new Error('x'))).toBe(false);
  });
});
