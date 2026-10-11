import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import { StatusPill } from '../../shared/ui/status-pill';
import type { StepItem } from './chat-model';
import { argLabel, argValue, resultText, toolLabel } from './tool-labels';

/** How each step state reads, as a word and a mark (never colour alone). */
export const STEP_STATE: Record<StepItem['state'], { text: string; mark: string }> = {
  running: { text: 'Working', mark: '…' },
  waiting: { text: 'Waiting for you', mark: '?' },
  done: { text: 'Done', mark: '✓' },
  failed: { text: 'Failed', mark: '✗' },
  declined: { text: 'Not run', mark: '–' },
};

/**
 * One tool the assistant used, as a step of its answer: what it did, with
 * which inputs, and a fold with what it saw.
 */
@Component({
  selector: 'app-chat-step',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [StatusPill],
  template: `
    @let s = step();
    <div class="step" [attr.data-state]="s.state">
      <span class="label">{{ label() }}</span>
      <app-status-pill
        class="state"
        [status]="s.state === 'declined' ? 'skipped' : s.state"
        [label]="state().text"
      />
    </div>
    @if (s.error && s.state !== 'running') {
      <p class="step-error">{{ s.error }}</p>
    }
    @if (argRows().length || hasResult()) {
      <details class="more">
        <summary>Details</summary>
        @if (argRows().length) {
          <dl class="args">
            @for (a of argRows(); track a.name) {
              <div>
                <dt>{{ a.name }}</dt>
                <dd class="num">{{ a.value }}</dd>
              </div>
            }
          </dl>
        }
        @if (hasResult()) {
          <p class="seen">What it saw</p>
          <pre class="num result" tabindex="0" aria-label="What the tool answered">{{
            result()
          }}</pre>
        }
      </details>
    }
  `,
  styles: `
    @use 'breakpoints' as bp;
    // A tool step: an inset well, the state as a working or receipt pill.
    :host {
      display: block;
      min-width: 0;
      max-width: min(100%, 46rem);
      padding: var(--space-2) var(--space-3);
      border-radius: var(--radius-sm);
      background: var(--color-surface-2);
      font-size: var(--text-sm);
    }
    .step {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: var(--space-1) var(--space-2);
    }
    .label {
      font-weight: var(--weight-medium);
      overflow-wrap: anywhere;
    }
    .state {
      margin-left: auto;
    }
    .step-error {
      margin: var(--space-1) 0 0;
      color: var(--color-ink-2);
      overflow-wrap: anywhere;
    }
    .more {
      margin-top: var(--space-1);
    }
    summary {
      display: inline-flex;
      align-items: center;
      min-height: var(--control-h);
      color: var(--color-ink-2);
      font-size: var(--text-xs);
      font-weight: var(--weight-medium);
      cursor: pointer;
      @include bp.phone {
        min-height: var(--touch-min);
      }
      @include bp.coarse {
        min-height: var(--touch-min);
      }
    }
    .args {
      display: grid;
      gap: var(--space-1);
      margin: var(--space-2) 0;
    }
    .args div {
      display: grid;
      grid-template-columns: minmax(6rem, auto) minmax(0, 1fr);
      gap: var(--space-2);
    }
    .args dt {
      color: var(--color-ink-3);
      font-size: var(--text-xs);
    }
    .args dd {
      margin: 0;
      overflow-wrap: anywhere;
    }
    .seen {
      margin: var(--space-2) 0 var(--space-1);
      color: var(--color-ink-3);
      font-size: var(--text-xs);
    }
    .result {
      max-height: 16rem;
      margin: 0;
      padding: var(--space-2);
      overflow: auto;
      border: 1px solid var(--color-border);
      border-radius: var(--radius-xs);
      background: var(--color-surface);
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      font-size: var(--text-xs);
    }
  `,
})
export class ChatStep {
  readonly step = input.required<StepItem>();

  protected readonly label = computed(() => toolLabel(this.step().tool));
  protected readonly state = computed(() => STEP_STATE[this.step().state]);
  protected readonly argRows = computed(() =>
    Object.entries(this.step().args).map(([name, value]) => ({
      name: argLabel(name),
      value: argValue(value),
    })),
  );
  protected readonly hasResult = computed(
    () => this.step().state === 'done' && this.step().result !== undefined,
  );
  protected readonly result = computed(() => resultText(this.step().result));
}
