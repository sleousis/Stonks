import { ChangeDetectionStrategy, Component, viewChild } from '@angular/core';
import { type ComponentFixture, TestBed } from '@angular/core/testing';

import type { BrokerInfo, GoLiveReport, StatusChangeRequest } from '../api/models';
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
import {
  HELD_POSITIONS_LINE,
  type PromotionSteps,
  demoteOptions,
  isGoLiveRefusal,
  promoteThroughGate,
} from './governance';
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

  function start(extra: Partial<PromotionSteps<string>> = {}) {
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
      ...extra,
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

describe('promoteThroughGate ticket (UX-03)', () => {
  let fixture: ComponentFixture<Host>;
  let el: HTMLElement;
  const alpaca = (paper: boolean): BrokerInfo => ({
    kind: 'alpaca',
    paper,
    allow_live: true,
    credentials_configured: true,
  });

  beforeEach(async () => {
    fixture = TestBed.createComponent(Host);
    await fixture.whenStable();
    el = fixture.nativeElement;
  });

  async function open(broker: BrokerInfo, extra: Partial<PromotionSteps<string>> = {}) {
    const result = promoteThroughGate({
      id: 'momentum_3fa9c21b',
      name: 'Momentum 3fa9',
      dialog: fixture.componentInstance.dialog(),
      toasts: { error: vi.fn() } as unknown as ToastService,
      golive: () => Promise.resolve(goLiveReport('momentum_3fa9c21b', true)),
      promote: () => Promise.resolve('done'),
      broker: () => Promise.resolve(broker),
      followers: () => Promise.resolve(['Main book']),
      title: 'Go live with Momentum 3fa9?',
      message: 'It places orders from the next trading run.',
      confirmLabel: 'Go live',
      ...extra,
    });
    await tick();
    fixture.detectChanges();
    return { result, form: dialogForm(el)! };
  }

  it('LIVE stamp and ticket lines when broker.paper is false, PAPER otherwise', async () => {
    const live = await open(alpaca(false));
    const ticket = live.form.querySelector('.ticket')!;
    expect(ticket.classList).toContain('live');
    expect(ticket.querySelector('app-mode-stamp')!.textContent).toContain('LIVE');
    expect(ticket.textContent).toContain('Momentum 3fa9');
    expect(ticket.textContent).toContain('Main book');
    expect(ticket.textContent).toContain('Alpaca live account');
    expect(live.form.textContent).toContain('Real money');
    // Real money needs the name typed, not a hold.
    expect(isHoldDialog(el)).toBe(false);

    const paper = await open(alpaca(true));
    const paperTicket = paper.form.querySelector('.ticket')!;
    expect(paperTicket.classList).not.toContain('live');
    expect(paperTicket.querySelector('app-mode-stamp')!.textContent).toContain('PAPER');
    expect(paper.form.textContent).toContain('no real money moves');
    expect(paper.form.textContent).not.toContain('Real money');
    expect(isHoldDialog(el)).toBe(true);
    await expect(live.result).resolves.toBeNull();
  });

  it('asks for the override straight away for an admin override', async () => {
    const promote = vi.fn().mockResolvedValue('forced');
    const { result, form } = await open(alpaca(true), { overrideFirst: true, promote });
    expect(form.textContent).toContain('without passing the check');
    answerDialog(fixture, { reason: 'Board approved the early start', typed: 'override' });
    await expect(result).resolves.toBe('forced');
    expect(promote).toHaveBeenCalledWith({
      reason: 'Board approved the early start',
      override: true,
    });
  });
});

describe('demoteOptions (UX-24)', () => {
  it('uses one title, message and tone for each step back', () => {
    const pause = demoteOptions('pause', 'Momentum 3fa9');
    expect(pause.title).toBe('Move Momentum 3fa9 back to paper trading?');
    expect(pause.confirmLabel).toBe('Back to paper trading');
    expect(pause.message).toContain(HELD_POSITIONS_LINE);
    const stop = demoteOptions('stop', 'Momentum 3fa9');
    expect(stop.title).toBe('Stop Momentum 3fa9?');
    expect(stop.tone).toBe('danger');
    expect(stop.message).toContain(HELD_POSITIONS_LINE);
  });
});

describe('isGoLiveRefusal', () => {
  it('is true only for a 409 ApiError', () => {
    expect(isGoLiveRefusal(refusal())).toBe(true);
    expect(isGoLiveRefusal(new ApiError(400, 'x', 'y'))).toBe(false);
    expect(isGoLiveRefusal(new Error('x'))).toBe(false);
  });
});
