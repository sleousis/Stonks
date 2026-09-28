import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';

import type { TurnView } from '../../api/models';
import { formatDateTime } from '../../core/format/format';
import { Sheet } from '../../shared/ui/sheet';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import { argLabel, argValue, resultText, toolLabel } from './tool-labels';

/** A turn's outcome in words. */
export function turnStatusText(status: string): string {
  switch (status) {
    case 'done':
      return 'Answered';
    case 'paused':
      return 'Waited for your yes';
    case 'running':
      return 'Still running';
    case 'timeout':
      return 'Ran out of time';
    case 'max_steps':
      return 'Stopped after too many steps';
    case 'model_error':
      return 'The model server failed';
    case 'tools_unavailable':
      return 'The tools could not load';
    default:
      return 'Stopped';
  }
}

/** "Model llama3.1, prompt v2, 3 steps, 1 order draft". */
export function turnMeta(t: TurnView): string {
  const parts = [`Model ${t.model}`, `prompt ${t.prompt_version}`];
  parts.push(`${t.steps} ${t.steps === 1 ? 'step' : 'steps'}`);
  const drafts = t.draft_ids.length;
  if (drafts) parts.push(`${drafts} order ${drafts === 1 ? 'draft' : 'drafts'}`);
  return parts.join(', ');
}

export interface TraceRow {
  tool: string;
  args: { name: string; value: string }[];
  outcome: string;
  detail: string;
}

/** One trace entry of a turn in words. */
export function traceRow(entry: Record<string, unknown>): TraceRow {
  const tool = typeof entry['tool'] === 'string' ? entry['tool'] : 'tool';
  const raw = entry['arguments'];
  const args =
    typeof raw === 'object' && raw !== null && !Array.isArray(raw)
      ? Object.entries(raw as Record<string, unknown>).map(([name, value]) => ({
          name: argLabel(name),
          value: argValue(value, 80),
        }))
      : [];
  let outcome: string;
  let detail = '';
  if (typeof entry['pending_action'] === 'string') {
    outcome = 'Asked for your yes';
  } else if (entry['ok'] === true) {
    outcome = entry['confirmed'] === true ? 'Ran after your yes' : 'Ran';
    detail = resultText(entry['result'], 1500);
  } else {
    outcome = 'Failed';
    detail = typeof entry['error'] === 'string' ? entry['error'] : '';
  }
  return { tool: toolLabel(tool), args, outcome, detail };
}

/**
 * The trace of a conversation: for each turn, the model and prompt version
 * it used, how it ended, and every tool it called with the inputs and what
 * came back. Opened from the conversation header.
 */
@Component({
  selector: 'app-trace-sheet',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [Sheet, LoadingState, EmptyState, ErrorState],
  template: `
    <app-sheet [open]="open()" [wide]="true" labelledBy="trace-title" (dismiss)="closed.emit()">
      <div class="sheet-form trace">
        <h2 id="trace-title">How the assistant got there</h2>
        @if (error(); as err) {
          <app-error-state title="Could not load the trace" [error]="err" (retry)="retry.emit()" />
        } @else if (turns() === null) {
          <app-loading-state label="Loading the trace" [rows]="3" />
        } @else if (rows().length === 0) {
          <app-empty-state
            title="No turns yet"
            message="Each answer you get is recorded here, with every tool it used."
          />
        } @else {
          <ol class="turns">
            @for (t of rows(); track t.id) {
              <li class="turn">
                <p class="turn-head">
                  <span class="num">{{ t.started }}</span>
                  <span>{{ t.status }}</span>
                </p>
                <p class="muted meta">{{ t.meta }}</p>
                @if (t.calls.length === 0) {
                  <p class="muted">No tools used.</p>
                } @else {
                  <ol class="calls">
                    @for (c of t.calls; track $index) {
                      <li>
                        <strong>{{ c.tool }}</strong> <span class="muted">{{ c.outcome }}</span>
                        @if (c.args.length) {
                          <dl class="args">
                            @for (a of c.args; track a.name) {
                              <div>
                                <dt>{{ a.name }}</dt>
                                <dd class="num">{{ a.value }}</dd>
                              </div>
                            }
                          </dl>
                        }
                        @if (c.detail) {
                          <pre class="num" tabindex="0" [attr.aria-label]="c.tool + ' answer'">{{
                            c.detail
                          }}</pre>
                        }
                      </li>
                    }
                  </ol>
                }
              </li>
            }
          </ol>
        }
        <div class="sheet-actions">
          <button type="button" class="btn" (click)="closed.emit()">Close</button>
        </div>
      </div>
    </app-sheet>
  `,
  styles: `
    .trace {
      max-height: calc(100dvh - 64px);
      overflow: auto;
    }
    .turns,
    .calls {
      display: grid;
      gap: var(--space-3);
      margin: 0;
      padding: 0;
      list-style: none;
    }
    .turn {
      padding: var(--space-3);
      border: 1px solid var(--color-border);
      border-radius: var(--radius-sm);
    }
    .turn-head {
      display: flex;
      flex-wrap: wrap;
      justify-content: space-between;
      gap: var(--space-2);
      font-weight: var(--weight-semibold);
    }
    .meta {
      margin: var(--space-1) 0 var(--space-2);
      font-size: var(--text-sm);
    }
    .calls li {
      padding-left: var(--space-3);
      border-left: 2px solid var(--color-border-strong);
      font-size: var(--text-sm);
      min-width: 0;
    }
    .args {
      display: grid;
      gap: 2px;
      margin: var(--space-1) 0;
    }
    .args div {
      display: grid;
      grid-template-columns: minmax(6rem, auto) minmax(0, 1fr);
      gap: var(--space-2);
    }
    .args dt {
      color: var(--color-ink-2);
    }
    .args dd {
      margin: 0;
      overflow-wrap: anywhere;
    }
    pre {
      max-height: 10rem;
      overflow: auto;
      margin: var(--space-1) 0 0;
      padding: var(--space-2);
      border: 1px solid var(--color-border);
      border-radius: var(--radius-xs);
      background: var(--color-surface-2);
      font-size: var(--text-xs);
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }
  `,
})
export class TraceSheet {
  readonly open = input.required<boolean>();
  /** null while loading. */
  readonly turns = input<readonly TurnView[] | null>(null);
  readonly error = input<unknown>(null);
  readonly closed = output<void>();
  readonly retry = output<void>();

  protected readonly rows = computed(() =>
    [...(this.turns() ?? [])].reverse().map((t) => ({
      id: t.id,
      started: formatDateTime(t.started_at),
      status: turnStatusText(t.status),
      meta: turnMeta(t),
      calls: t.trace.map(traceRow),
    })),
  );
}
