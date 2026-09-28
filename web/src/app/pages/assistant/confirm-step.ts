import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';

import type { ConfirmItem } from './chat-model';
import { argLabel, argValue, isDangerTool, resultText, toolLabel } from './tool-labels';

/** What a tool's own preview says, when it sends warnings or a summary. */
export function previewLines(preview: unknown): string[] {
  if (preview === null || preview === undefined || preview === '') return [];
  if (typeof preview === 'string') return [preview];
  if (typeof preview !== 'object' || Array.isArray(preview)) return [];
  const p = preview as Record<string, unknown>;
  const lines: string[] = [];
  for (const key of ['summary', 'message', 'detail']) {
    if (typeof p[key] === 'string' && p[key]) lines.push(p[key] as string);
  }
  if (Array.isArray(p['warnings'])) {
    for (const w of p['warnings']) if (typeof w === 'string' && w) lines.push(w);
  }
  return lines;
}

/**
 * A write action the assistant wants to run, shown as a ticket: what it
 * will do, with which inputs, and the tool's own preview. Nothing runs
 * until you approve it here.
 */
@Component({
  selector: 'app-confirm-step',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @let c = item();
    <section class="ticket confirm" [class.danger]="danger()" [attr.aria-labelledby]="titleId()">
      <div class="ticket-head">
        <span class="ticket-kind">Needs your yes</span>
        <span class="decided">{{ decidedText() }}</span>
      </div>
      <h3 [id]="titleId()">{{ label() }}</h3>
      @if (c.description) {
        <p class="about">{{ c.description }}</p>
      }
      @if (argRows().length) {
        <dl class="ticket-lines">
          @for (a of argRows(); track a.name) {
            <div>
              <dt>{{ a.name }}</dt>
              <dd>{{ a.value }}</dd>
            </div>
          }
        </dl>
      }
      @if (lines().length) {
        <ul class="preview" aria-label="What it will do">
          @for (line of lines(); track $index) {
            <li>{{ line }}</li>
          }
        </ul>
      } @else if (previewText()) {
        <details>
          <summary>Preview</summary>
          <pre class="num" tabindex="0" aria-label="The tool's preview">{{ previewText() }}</pre>
        </details>
      }
      @if (c.state === 'pending') {
        <p class="hint">Nothing runs until you approve it.</p>
        <div class="actions">
          <button type="button" class="btn" [disabled]="busy()" (click)="decide.emit(false)">
            Reject
          </button>
          <button
            type="button"
            class="btn"
            [class.btn-primary]="!danger()"
            [class.btn-danger]="danger()"
            [disabled]="busy()"
            [attr.aria-busy]="busy()"
            (click)="decide.emit(true)"
          >
            Approve and run
          </button>
        </div>
      }
    </section>
  `,
  styles: `
    @use 'breakpoints' as bp;
    :host {
      display: block;
      min-width: 0;
    }
    .confirm.danger {
      border-color: var(--color-loss);
    }
    h3 {
      font-size: var(--text-md);
      overflow-wrap: anywhere;
    }
    .decided {
      font-size: var(--text-xs);
      font-weight: var(--weight-semibold);
      color: var(--color-ink-2);
    }
    .about {
      color: var(--color-ink-2);
      font-size: var(--text-sm);
      white-space: pre-line;
      overflow-wrap: anywhere;
    }
    .preview {
      margin: 0;
      padding-left: var(--space-5);
      font-size: var(--text-sm);
    }
    pre {
      max-height: 12rem;
      overflow: auto;
      margin: var(--space-1) 0 0;
      font-size: var(--text-xs);
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }
    summary {
      display: inline-flex;
      align-items: center;
      min-height: 24px;
      cursor: pointer;
      @include bp.phone {
        min-height: var(--touch-min);
      }
    }
    .hint {
      color: var(--color-ink-3);
      font-size: var(--text-xs);
    }
    .actions {
      display: flex;
      flex-wrap: wrap;
      justify-content: flex-end;
      gap: var(--space-2);
      @include bp.phone {
        flex-direction: column-reverse;
        .btn {
          width: 100%;
        }
      }
    }
  `,
})
export class ConfirmStep {
  readonly item = input.required<ConfirmItem>();
  readonly busy = input(false);
  /** true approves (runs it), false rejects. */
  readonly decide = output<boolean>();

  protected readonly titleId = computed(() => `confirm-${this.item().actionId}`);
  protected readonly label = computed(() => toolLabel(this.item().tool));
  protected readonly danger = computed(() => isDangerTool(this.item().tool));
  protected readonly argRows = computed(() =>
    Object.entries(this.item().args).map(([name, value]) => ({
      name: argLabel(name),
      value: argValue(value),
    })),
  );
  protected readonly lines = computed(() => previewLines(this.item().preview));
  protected readonly previewText = computed(() => {
    const p = this.item().preview;
    return p === null || p === undefined ? '' : resultText(p, 2000);
  });
  protected readonly decidedText = computed(() => {
    switch (this.item().state) {
      case 'approved':
        return '✓ Approved';
      case 'rejected':
        return '✗ Rejected';
      default:
        return '';
    }
  });
}
