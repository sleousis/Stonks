import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { RouterLink } from '@angular/router';

import type { GoLiveReport } from '../../api/models';
import { checklistItems, traderChecks } from '../../shared/golive-checks';
import { HelpTip } from '../../shared/ui/help-tip';
import { StatusPill } from '../../shared/ui/status-pill';

/** Where the go-live check read the trial result from, in plain words. */
export function sourceWords(source: GoLiveReport['source']): string {
  if (source === 'shadow') return 'its own test book';
  if (source === 'portfolio') return 'the shared portfolio';
  return 'nowhere yet (no trial record)';
}

/**
 * Checks whose limit measures a bad result (a drop too deep, drift too
 * wide): those failed. Every other open check only needs time or a step
 * (more trial days, a full test run), so it reads as a neutral "Not yet".
 */
const FAILED_CHECKS: ReadonlySet<string> = new Set([
  'max_drawdown',
  'within_mc_band',
  'quit_rule',
  'max_drift',
]);

/** True when an open check measured a bad result (red), not just "not yet". */
export function isFailedCheck(name: string): boolean {
  return FAILED_CHECKS.has(name);
}

/**
 * The go-live check of one strategy as a trader reads it (R13): how many
 * checks pass, then each failing check in one plain sentence with how to fix
 * it. The checklist a reviewer reads before approving folds away below (it
 * never changes the verdict). The strategy page's Review tab.
 */
@Component({
  selector: 'app-golive-check-list',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, HelpTip, StatusPill],
  template: `
    <p class="summary panel-body">
      @if (checks().failing.length === 0) {
        All {{ checks().total }} checks pass.
      } @else {
        {{ checks().passed }} of {{ checks().total }} checks pass.
      }
      <span class="source">Trial result from {{ source() }}.</span>
    </p>
    @if (checks().failing.length) {
      <ul class="checks" aria-label="Checks that do not pass yet">
        @for (c of checks().failing; track c.name) {
          <li>
            <div class="check-head">
              <span class="check-label">{{ c.label }}</span>
              @if (failed(c.name)) {
                <app-status-pill status="fail" label="Failed" />
              } @else {
                <app-status-pill status="not_yet" />
              }
            </div>
            <div class="check-body">
              <p class="check-detail">{{ c.sentence }}</p>
              @if (c.fix; as f) {
                <div class="check-fix">
                  <p>{{ f.text }}</p>
                  @if (f.link; as l) {
                    <a
                      class="btn btn-tertiary"
                      [routerLink]="l.commands"
                      [queryParams]="l.query ?? null"
                      >{{ l.label }}</a
                    >
                  }
                </div>
              }
            </div>
          </li>
        }
      </ul>
    }
    <details class="checklist panel-body">
      <summary>Before you approve</summary>
      <p class="muted checklist-lead">
        What a reviewer reads before approving. It does not change the verdict.
      </p>
      <dl class="checklist-grid">
        @for (item of checklist(); track item.key) {
          <div [class.text]="item.text" [class.missing]="!item.recorded">
            <dt>
              {{ item.label }}
              @if (item.help) {
                <app-help-tip [term]="item.help" />
              }
            </dt>
            <dd [class.num]="!item.text">{{ item.value }}</dd>
          </div>
        }
      </dl>
    </details>
  `,
  styles: `
    @use 'breakpoints' as bp;

    :host {
      display: block;
      min-width: 0;
    }
    .summary {
      display: grid;
      gap: var(--space-1);
      font-size: var(--text-sm);
      font-weight: var(--weight-medium);
    }
    .source {
      color: var(--color-ink-2);
      font-weight: var(--weight-regular);
    }
    .checks {
      margin: 0;
      padding: 0 var(--space-4) var(--space-2);
      list-style: none;
      border-top: 1px solid var(--color-border);
    }
    /* One row per check: title row, 12px, then the body. Hairlines between. */
    .checks li {
      display: grid;
      gap: var(--space-3);
      min-width: 0;
      padding: var(--space-4) 0;
      border-top: 1px solid var(--color-border);
    }
    .checks li:first-child {
      border-top: 0;
    }
    .check-head {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: var(--space-2);
    }
    .check-label {
      font-weight: var(--weight-semibold);
    }
    .check-body {
      display: grid;
      gap: var(--space-1);
      max-width: 70ch;
    }
    .check-detail {
      font-size: var(--text-sm);
      overflow-wrap: anywhere;
    }
    .check-fix {
      display: grid;
      justify-items: start;
      gap: var(--space-2);
      color: var(--color-ink-2);
      font-size: var(--text-sm);
    }
    /* The link is a quiet button, nudged so its text lines up with the text above. */
    .check-fix .btn {
      margin-inline-start: calc(-1 * var(--space-3));
    }
    .checklist {
      display: grid;
      gap: var(--space-2);
      border-top: 1px solid var(--color-border);
    }
    .checklist summary {
      display: flex;
      align-items: center;
      min-height: var(--touch-min);
      font-weight: var(--weight-semibold);
      cursor: pointer;
    }
    .checklist-lead {
      font-size: var(--text-sm);
    }
    .checklist-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: var(--space-2);
      margin: 0;
    }
    @include bp.from-tablet {
      .checklist-grid {
        grid-template-columns: repeat(4, minmax(0, 1fr));
      }
    }
    .checklist-grid > div {
      display: grid;
      align-content: start;
      gap: var(--space-1);
      min-width: 0;
      padding: var(--space-2) var(--space-3);
      border-radius: var(--radius-sm);
      background: var(--color-surface-2);
    }
    .checklist-grid > div.text {
      grid-column: 1 / -1;
    }
    .checklist-grid dt {
      color: var(--color-ink-3);
      font-size: var(--text-xs);
    }
    .checklist-grid dd {
      margin: 0;
      font-weight: var(--weight-medium);
      overflow-wrap: anywhere;
    }
    .checklist-grid .text dd {
      max-width: 70ch;
      font-weight: var(--weight-regular);
    }
    .checklist-grid .missing dd {
      color: var(--color-ink-3);
    }
  `,
})
export class GoliveCheckList {
  readonly report = input.required<GoLiveReport>();

  protected readonly checks = computed(() => traderChecks(this.report()));
  protected readonly checklist = computed(() => checklistItems(this.report().checklist));
  protected readonly source = computed(() => sourceWords(this.report().source));

  protected readonly failed = isFailedCheck;
}
