import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
  output,
  signal,
} from '@angular/core';

import type { IntervalInfo, SignalIcRequest, StrategyClassInfo } from '../../api/models';
import { SessionService } from '../../core/auth/session.service';
import { PermissionNote } from '../../shared/ui/permission-note';
import type { WindowForm } from './lab-requests';
import {
  type SignalIcForm,
  buildSignalIcRequest,
  defaultSignalIcForm,
  signalIcErrors,
} from './research-requests';
import { StrategyPicker } from './strategy-picker';
import { WindowFields } from './window-fields';

/** Pick a strategy and tickers to measure how well its scores rank the next moves. */
@Component({
  selector: 'app-signal-ic-form',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [StrategyPicker, WindowFields, PermissionNote],
  template: `
    <form class="lab-form" novalidate (submit)="$event.preventDefault(); submit()">
      <app-strategy-picker
        idPrefix="ic"
        [classes]="classes()"
        [value]="form().classPath"
        [error]="errors()['strategy'] ?? null"
        (valueChange)="patch({ classPath: $event })"
      />
      <fieldset class="group">
        <legend>Data</legend>
        <app-window-fields
          idPrefix="ic"
          [value]="form()"
          [errors]="errors()"
          [intervals]="intervals()"
          (patch)="patch($event)"
        />
        <div class="field">
          <label for="ic-horizons">Look ahead (bars)</label>
          <input
            id="ic-horizons"
            class="input num"
            inputmode="numeric"
            autocomplete="off"
            spellcheck="false"
            placeholder="1, 5, 21"
            aria-describedby="ic-horizons-hint"
            [value]="form().horizons"
            [attr.aria-invalid]="!!errors()['horizons']"
            (input)="patch({ horizons: $any($event.target).value })"
          />
          @if (errors()['horizons']; as e) {
            <span class="error" id="ic-horizons-hint">{{ e }}</span>
          } @else {
            <span class="hint" id="ic-horizons-hint">
              How far ahead to check each score, in bars. Blank checks the usual spread.
            </span>
          }
        </div>
      </fieldset>
      <div class="form-actions">
        @if (errorCount(); as n) {
          <p class="error summary" role="alert">
            Fix {{ n === 1 ? 'the highlighted field' : 'the ' + n + ' highlighted fields' }} to run
            it.
          </p>
        }
        <button
          type="submit"
          class="btn btn-primary"
          [disabled]="busy() || !canRun()"
          [attr.aria-busy]="busy()"
        >
          {{ busy() ? 'Starting…' : 'Measure signal' }}
        </button>
        <app-permission-note permission="lab.run" />
      </div>
    </form>
  `,
  styleUrls: ['./lab-form.scss'],
})
export class SignalIcFormView {
  readonly classes = input.required<readonly StrategyClassInfo[]>();
  readonly intervals = input<readonly IntervalInfo[]>([]);
  readonly busy = input(false);
  readonly submitted = output<SignalIcRequest>();

  private readonly session = inject(SessionService);
  protected readonly canRun = computed(() => this.session.can('lab.run'));

  protected readonly form = signal<SignalIcForm>(defaultSignalIcForm());
  protected readonly tried = signal(false);
  private readonly allErrors = computed(() => signalIcErrors(this.form()));
  protected readonly errors = computed(() => (this.tried() ? this.allErrors() : {}));
  protected readonly errorCount = computed(() => Object.keys(this.errors()).length);

  protected patch(p: Partial<SignalIcForm> | Partial<WindowForm>): void {
    this.form.update((f) => ({ ...f, ...p }));
  }

  protected submit(): void {
    if (!this.canRun()) return;
    this.tried.set(true);
    if (Object.keys(this.allErrors()).length) return;
    this.submitted.emit(buildSignalIcRequest(this.form()));
  }
}
