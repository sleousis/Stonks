import {
  ChangeDetectionStrategy,
  Component,
  type ElementRef,
  effect,
  inject,
  linkedSignal,
  signal,
  viewChild,
} from '@angular/core';

import { errorMessage } from '../http/api-error';
import { StepUpService } from './step-up.service';

/**
 * Renders StepUpService requests: one code field (or a recovery code) in a
 * modal <dialog>. Mounted once, in the shell. A full-screen sheet on phones.
 */
@Component({
  selector: 'app-step-up-dialog',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <dialog
      #dialog
      class="sheet"
      aria-labelledby="step-up-title"
      aria-describedby="step-up-reason"
      (cancel)="$event.preventDefault(); cancel()"
    >
      @if (request(); as req) {
        <form (submit)="$event.preventDefault(); submit()">
          <h2 id="step-up-title">Confirm it is you</h2>
          <p id="step-up-reason" class="reason">{{ req.reason }}</p>
          <div class="field">
            @if (useRecovery()) {
              <label for="step-up-recovery">Recovery code</label>
              <input
                id="step-up-recovery"
                class="input"
                autocomplete="off"
                autocapitalize="off"
                spellcheck="false"
                maxlength="64"
                [value]="value()"
                (input)="value.set($any($event.target).value)"
              />
            } @else {
              <label for="step-up-code">Code from your authenticator app</label>
              <input
                id="step-up-code"
                class="input code"
                inputmode="numeric"
                autocomplete="one-time-code"
                maxlength="8"
                [value]="value()"
                (input)="value.set($any($event.target).value)"
              />
            }
            @if (error(); as err) {
              <span class="error" role="alert">{{ err }}</span>
            }
          </div>
          <button type="button" class="btn btn-ghost switch" (click)="toggleRecovery()">
            {{ useRecovery() ? 'Use the app code instead' : 'Use a recovery code' }}
          </button>
          <div class="actions">
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
    </dialog>
  `,
  styles: `
    @use 'breakpoints' as bp;

    .sheet {
      width: min(420px, calc(100vw - 32px));
      padding: 0;
      border: 1px solid var(--color-border);
      border-radius: var(--radius-lg);
      background: var(--color-surface);
      color: var(--color-ink);
      box-shadow: var(--shadow-2);
    }
    .sheet::backdrop {
      background: var(--color-scrim);
    }
    form {
      display: grid;
      gap: var(--space-4);
      padding: var(--space-5);
    }
    h2 {
      font-size: var(--text-lg);
    }
    .reason {
      color: var(--color-ink-2);
    }
    .code {
      font-size: var(--text-lg);
      letter-spacing: 0.2em;
      font-variant-numeric: tabular-nums;
    }
    .switch {
      justify-self: start;
    }
    .actions {
      display: flex;
      justify-content: flex-end;
      gap: var(--space-2);
    }
    @include bp.phone {
      .sheet {
        width: 100vw;
        max-width: 100vw;
        height: 100dvh;
        max-height: 100dvh;
        margin: 0;
        border: 0;
        border-radius: 0;
      }
      form {
        min-height: 100%;
        align-content: start;
        padding: calc(var(--space-5) + env(safe-area-inset-top)) var(--space-4)
          calc(var(--space-4) + env(safe-area-inset-bottom));
      }
      .actions {
        margin-top: auto;
        flex-direction: column-reverse;
      }
      .actions .btn {
        width: 100%;
      }
    }
  `,
})
export class StepUpDialog {
  private readonly stepUp = inject(StepUpService);
  private readonly dialog = viewChild.required<ElementRef<HTMLDialogElement>>('dialog');

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
      const el = this.dialog().nativeElement;
      if (this.request()) {
        if (!el.open) el.showModal?.();
      } else if (el.open) {
        el.close();
      }
    });
  }

  protected toggleRecovery(): void {
    this.useRecovery.update((v) => !v);
    this.value.set('');
    this.error.set(null);
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
}
