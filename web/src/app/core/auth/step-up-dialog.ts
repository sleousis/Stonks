import {
  ChangeDetectionStrategy,
  Component,
  type ElementRef,
  Injector,
  afterNextRender,
  effect,
  inject,
  linkedSignal,
  signal,
  untracked,
  viewChild,
} from '@angular/core';

import { Sheet } from '../../shared/ui/sheet';
import { errorMessage } from '../http/api-error';
import { StepUpService } from './step-up.service';

/**
 * Renders StepUpService requests: one code field (or a recovery code) in the
 * shared `<app-sheet>` (a card on larger screens, full screen on phones,
 * Escape cancels). Mounted once, in the shell. Opens with the cursor in
 * the code field (UX-47).
 */
@Component({
  selector: 'app-step-up-dialog',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [Sheet],
  template: `
    <app-sheet
      [open]="!!request()"
      labelledBy="step-up-title"
      describedBy="step-up-reason"
      (dismiss)="cancel()"
    >
      @if (request(); as req) {
        <form class="sheet-form" (submit)="$event.preventDefault(); submit()">
          <h2 id="step-up-title">Confirm it is you</h2>
          <p id="step-up-reason" class="sheet-message">{{ req.reason }}</p>
          <div class="field">
            @if (useRecovery()) {
              <label for="step-up-recovery">Recovery code</label>
              <input
                #codeInput
                id="step-up-recovery"
                class="input"
                autocomplete="off"
                autocapitalize="off"
                spellcheck="false"
                maxlength="64"
                [attr.aria-invalid]="error() ? true : null"
                [attr.aria-describedby]="error() ? 'step-up-error' : null"
                [value]="value()"
                (input)="value.set($any($event.target).value)"
              />
            } @else {
              <label for="step-up-code">Code from your authenticator app</label>
              <input
                #codeInput
                id="step-up-code"
                class="input code"
                inputmode="numeric"
                autocomplete="one-time-code"
                maxlength="8"
                [attr.aria-invalid]="error() ? true : null"
                [attr.aria-describedby]="error() ? 'step-up-error' : null"
                [value]="value()"
                (input)="value.set($any($event.target).value)"
              />
            }
            @if (error(); as err) {
              <span id="step-up-error" class="error" role="alert">{{ err }}</span>
            }
          </div>
          <button type="button" class="btn btn-ghost switch" (click)="toggleRecovery()">
            {{ useRecovery() ? 'Use the app code instead' : 'Use a recovery code' }}
          </button>
          <div class="sheet-actions">
            <button type="button" class="btn" (click)="cancel()">Cancel</button>
            <button
              type="submit"
              class="btn btn-primary"
              [disabled]="busy() || !value().trim()"
              [attr.aria-busy]="busy()"
            >
              {{ busy() ? 'Checking…' : 'Confirm' }}
            </button>
          </div>
        </form>
      }
    </app-sheet>
  `,
  styles: `
    .code {
      font-size: var(--text-lg);
      letter-spacing: 0.2em;
      font-variant-numeric: tabular-nums;
    }
    .switch {
      justify-self: start;
    }
  `,
})
export class StepUpDialog {
  private readonly stepUp = inject(StepUpService);
  private readonly injector = inject(Injector);
  private readonly codeInput = viewChild<ElementRef<HTMLInputElement>>('codeInput');

  protected readonly request = this.stepUp.request;
  protected readonly value = linkedSignal({ source: this.request, computation: () => '' });
  protected readonly useRecovery = linkedSignal({
    source: this.request,
    computation: () => false,
  });
  protected readonly error = linkedSignal<unknown, string | null>({
    source: this.request,
    computation: () => null,
  });
  protected readonly busy = signal(false);

  constructor() {
    effect(() => {
      if (this.request()) untracked(() => this.focusCode());
    });
  }

  protected toggleRecovery(): void {
    this.useRecovery.update((v) => !v);
    this.value.set('');
    this.error.set(null);
    this.focusCode();
  }

  protected async submit(): Promise<void> {
    const value = this.value().trim();
    if (!value || this.busy()) return;
    this.busy.set(true);
    this.error.set(null);
    try {
      await this.stepUp.submit(this.useRecovery() ? { recovery_code: value } : { code: value });
    } catch (err) {
      this.error.set(errorMessage(err));
    } finally {
      this.busy.set(false);
    }
  }

  protected cancel(): void {
    this.stepUp.cancel();
  }

  /** Once the field is on screen, put the cursor in it. */
  private focusCode(): void {
    afterNextRender(() => this.codeInput()?.nativeElement.focus(), { injector: this.injector });
  }
}
