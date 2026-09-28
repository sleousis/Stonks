import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { RouterLink } from '@angular/router';

import type { GoLiveReport } from '../../api/models';
import { type CheckRow, checkRow, checklistItems } from '../../shared/golive-checks';
import { HelpTip } from '../../shared/ui/help-tip';
import { StatusPill } from '../../shared/ui/status-pill';

/** Where the go-live check read the trial result from, in plain words. */
export function sourceWords(source: GoLiveReport['source']): string {
  if (source === 'shadow') return 'its own test book';
  if (source === 'portfolio') return 'the shared portfolio';
  return 'nowhere yet (no trial record)';
}

/**
 * The go-live check of one strategy, every check with its value against the
 * limit, what it measures and how to fix it, then the checklist a reviewer
 * reads before approving (it never changes the verdict). The strategy
 * page's Review tab.
 */
@Component({
  selector: 'app-golive-check-list',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, HelpTip, StatusPill],
  template: `
    <p class="summary panel-body">
      @if (report().passed) {
        All {{ rows().length }} checks passed.
      } @else {
        {{ failedCount() }} of {{ rows().length }} checks failed.
      }
      <span class="source">Trial result from {{ source() }}.</span>
    </p>
    <ul class="checks" aria-label="Go-live checks">
      @for (c of rows(); track c.name) {
        <li [class.failed]="!c.passed">
          <div class="check-head">
            <span class="check-label">{{ c.label }}</span>
            <app-status-pill [status]="c.passed ? 'pass' : 'fail'" />
          </div>
          @if (c.name !== 'status') {
            <dl class="check-figures">
              <div>
                <dt>Value</dt>
                <dd class="num check-value">{{ c.value }}</dd>
              </div>
              <div>
                <dt>Limit</dt>
                <dd class="num check-limit">{{ c.limit }}</dd>
              </div>
            </dl>
          }
          <p class="check-detail">{{ c.detail }}</p>
          <p class="check-measures">{{ c.measures }}</p>
          @if (c.fix; as f) {
            <p class="check-fix">
              {{ f.text }}
              @if (f.link; as l) {
                <a [routerLink]="l.commands" [queryParams]="l.query ?? null">{{ l.label }}</a>
              }
            </p>
          }
        </li>
      }
    </ul>
    <section class="checklist panel-body" aria-labelledby="checklist-title">
      <h3 id="checklist-title">Before you approve</h3>
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
    </section>
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
    }
    .source {
      color: var(--color-ink-2);
    }
    .checks {
      margin: 0;
      padding: 0 var(--space-4) var(--space-2);
      list-style: none;
      border-top: 1px solid var(--color-border);
    }
    .checks li {
      display: grid;
      gap: var(--space-1);
      min-width: 0;
      padding: var(--space-3) 0;
      border-top: 1px solid var(--color-border);
    }
    .checks li:first-child {
      border-top: 0;
    }
    @include bp.from-tablet {
      .checks li {
        grid-template-columns: 12rem 13rem minmax(0, 1fr);
        grid-template-areas:
          'head figures detail'
          'head figures measures'
          'head figures fix';
        column-gap: var(--space-4);
        align-items: start;
      }
      .check-head {
        grid-area: head;
      }
      .check-figures {
        grid-area: figures;
      }
      .check-detail {
        grid-area: detail;
      }
      .check-measures {
        grid-area: measures;
      }
      .check-fix {
        grid-area: fix;
      }
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
    .check-figures {
      display: flex;
      gap: var(--space-4);
      margin: 0;
    }
    .check-figures div {
      display: grid;
    }
    .check-figures dt {
      color: var(--color-ink-2);
      font-size: var(--text-xs);
    }
    .check-figures dd {
      margin: 0;
      font-size: var(--text-sm);
      font-weight: var(--weight-medium);
    }
    .check-detail {
      font-size: var(--text-sm);
      overflow-wrap: anywhere;
    }
    .check-measures {
      color: var(--color-ink-2);
      font-size: var(--text-xs);
    }
    .check-fix {
      font-size: var(--text-sm);
      font-weight: var(--weight-medium);
    }
    .check-fix a {
      display: inline-flex;
      align-items: center;
      min-height: var(--touch-min);
    }
    .checklist {
      display: grid;
      gap: var(--space-2);
      border-top: 1px solid var(--color-border);
    }
    .checklist h3 {
      font-size: var(--text-md);
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
      gap: 2px;
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

  protected readonly rows = computed<CheckRow[]>(() => {
    const r = this.report();
    return r.checks.map((c) => checkRow(c, r.strategy_id));
  });
  protected readonly failedCount = computed(() => this.rows().filter((r) => !r.passed).length);
  protected readonly checklist = computed(() => checklistItems(this.report().checklist));
  protected readonly source = computed(() => sourceWords(this.report().source));
}
