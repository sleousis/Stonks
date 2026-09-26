import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import type { PreflightIssueView, PreflightView } from '../../api/models';
import { humanize } from '../ui/param-form/param-spec';
import { StatusPill } from '../ui/status-pill';

export interface IssueRow {
  code: string;
  label: string;
  severity: PreflightIssueView['severity'];
  message: string;
  details: string[];
}

function detailText(v: unknown): string {
  if (Array.isArray(v)) {
    const shown = v.slice(0, 8).map(String).join(', ');
    return v.length > 8 ? `${shown} and ${v.length - 8} more` : shown;
  }
  if (v !== null && typeof v === 'object') return JSON.stringify(v);
  return String(v);
}

/** Preflight issues as rows, errors first. Details read "Tickers: A, B". */
export function issueRows(p: PreflightView | null | undefined): IssueRow[] {
  if (!p) return [];
  const rank = (s: string) => (s === 'error' ? 0 : 1);
  return [...p.issues]
    .sort((a, b) => rank(a.severity) - rank(b.severity))
    .map((i) => ({
      code: i.code,
      label: humanize(i.code),
      severity: i.severity,
      message: i.message,
      details: Object.entries(i.details ?? {}).map(([k, v]) => `${humanize(k)}: ${detailText(v)}`),
    }));
}

/**
 * The data preflight of a lab run: survivorship, missing bars, flagged
 * statements. A run only starts without errors, so a result shows warnings.
 *
 *   <app-preflight-issues [preflight]="result.preflight" />
 */
@Component({
  selector: 'app-preflight-issues',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [StatusPill],
  template: `
    @let p = preflight();
    <section aria-labelledby="preflight-title">
      <h3 id="preflight-title">Data checks</h3>
      @if (!p) {
        <p class="muted note">This result has no data checks.</p>
      } @else if (p.skipped) {
        <p class="muted note">Data checks were skipped for this run.</p>
      } @else if (rows().length === 0) {
        <p class="note">Data checks passed with no warnings.</p>
      } @else {
        <ul class="issues">
          @for (i of rows(); track $index) {
            <li class="issue" [class.error]="i.severity === 'error'">
              <div class="issue-head">
                <app-status-pill
                  [status]="i.severity"
                  [label]="i.severity === 'error' ? 'Error' : 'Warning'"
                />
                <span class="code">{{ i.label }}</span>
              </div>
              <p class="message">{{ i.message }}</p>
              @for (d of i.details; track $index) {
                <p class="detail muted">{{ d }}</p>
              }
            </li>
          }
        </ul>
      }
    </section>
  `,
  styles: `
    :host {
      display: block;
      min-width: 0;
    }
    h3 {
      font-size: var(--text-md);
      margin-bottom: var(--space-2);
    }
    .note,
    .detail {
      font-size: var(--text-sm);
    }
    .issues {
      list-style: none;
      margin: 0;
      padding: 0;
      display: grid;
      gap: var(--space-2);
    }
    .issue {
      display: grid;
      gap: var(--space-1);
      padding: var(--space-3);
      border: 1px solid var(--color-border);
      border-left: 3px solid var(--color-warn);
      border-radius: var(--radius-sm);
      background: var(--color-surface);
      min-width: 0;
    }
    .issue.error {
      border-left-color: var(--color-loss);
    }
    .issue-head {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: var(--space-2);
    }
    .code {
      font-weight: var(--weight-semibold);
    }
    .message,
    .detail {
      overflow-wrap: anywhere;
    }
  `,
})
export class PreflightIssues {
  readonly preflight = input<PreflightView | null | undefined>(null);
  protected readonly rows = computed(() => issueRows(this.preflight()));
}
