import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { RouterLink } from '@angular/router';

import type { LabRunView } from '../../api/models';
import { formatNumber, formatPercent } from '../../core/format/format';
import { humanize } from '../../shared/ui/param-form/param-spec';
import { StatTile } from '../../shared/ui/stat-tile';
import { StatusPill } from '../../shared/ui/status-pill';
import { SURVIVAL_TESTS } from './lab-requests';

/** Metric keys shown as percentages (fractions from the API). */
const PERCENT_METRIC = /(return|drawdown|share|cagr|ratio_pct)$/;

export function formatMetric(key: string, value: number | null): string {
  if (value === null) return '–';
  if (PERCENT_METRIC.test(key)) return formatPercent(value);
  if (/p_value$/.test(key)) return formatNumber(value, { digits: 3 });
  return formatNumber(value, { digits: Number.isInteger(value) ? 0 : 3 });
}

export function testLabel(id: string): string {
  return SURVIVAL_TESTS.find((t) => t.id === id)?.label ?? humanize(id);
}

function paramText(v: unknown): string {
  if (typeof v === 'number') return formatNumber(v);
  if (typeof v === 'string') return v;
  return JSON.stringify(v);
}

/** Verdict, best parameters and one pass/fail row per survival test. */
@Component({
  selector: 'app-lab-run-result',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, StatTile, StatusPill],
  template: `
    @let r = result();
    <div class="summary">
      <app-stat-tile
        label="Verdict"
        featured
        [value]="r.verdict === 'pass' ? 'Passed' : 'Failed'"
        [detail]="passedText()"
        [detailTone]="r.verdict === 'pass' ? 'gain' : 'loss'"
      />
      <app-stat-tile label="Best score" [value]="score()" [detail]="r.class_path" />
    </div>

    @if (r.registered_strategy_id; as id) {
      <p class="registered">
        Registered in shadow as
        <a class="num" [routerLink]="['/strategies', id]">{{ id }}</a
        >.
      </p>
    }

    <section aria-labelledby="best-params-title">
      <h3 id="best-params-title">Best parameters</h3>
      @if (params().length) {
        <dl class="params">
          @for (p of params(); track p.name) {
            <div>
              <dt>{{ p.label }}</dt>
              <dd class="num">{{ p.value }}</dd>
            </div>
          }
        </dl>
      } @else {
        <p class="muted">The class has no tunable parameters.</p>
      }
    </section>

    <section aria-labelledby="survival-title">
      <h3 id="survival-title">Survival tests</h3>
      <ul class="tests">
        @for (t of tests(); track t.id) {
          <li class="test" [class.failed]="!t.passed">
            <div class="test-head">
              <span class="test-name">{{ t.label }}</span>
              <app-status-pill [status]="t.passed ? 'pass' : 'fail'" />
            </div>
            @if (t.metrics.length) {
              <dl class="metrics">
                @for (m of t.metrics; track m.key) {
                  <div>
                    <dt>{{ m.label }}</dt>
                    <dd class="num">{{ m.value }}</dd>
                  </div>
                }
              </dl>
            }
            @if (t.notes) {
              <p class="notes">{{ t.notes }}</p>
            }
          </li>
        } @empty {
          <li class="muted">No survival tests ran.</li>
        }
      </ul>
    </section>
  `,
  styles: `
    @use 'breakpoints' as bp;
    :host {
      display: grid;
      gap: var(--space-4);
      min-width: 0;
    }
    .summary {
      display: grid;
      gap: var(--space-3);
      grid-template-columns: minmax(0, 1fr);
      @include bp.from-tablet {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }
    }
    .registered {
      font-size: var(--text-sm);
    }
    h3 {
      font-size: var(--text-md);
      margin-bottom: var(--space-2);
    }
    dl {
      margin: 0;
      display: grid;
      gap: var(--space-2) var(--space-4);
      grid-template-columns: repeat(auto-fill, minmax(min(100%, 9rem), 1fr));
    }
    dt {
      font-size: var(--text-xs);
      color: var(--color-ink-3);
    }
    dd {
      margin: 0;
      font-weight: var(--weight-medium);
      overflow-wrap: anywhere;
      white-space: normal;
    }
    .tests {
      list-style: none;
      margin: 0;
      padding: 0;
      display: grid;
      gap: var(--space-2);
    }
    .test {
      display: grid;
      gap: var(--space-2);
      padding: var(--space-3);
      border: 1px solid var(--color-border);
      border-left: 3px solid var(--color-gain);
      border-radius: var(--radius-sm);
      background: var(--color-surface);
    }
    .test.failed {
      border-left-color: var(--color-loss);
    }
    .test-head {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: var(--space-2);
    }
    .test-name {
      font-weight: var(--weight-semibold);
    }
    .notes {
      font-size: var(--text-sm);
      color: var(--color-ink-2);
      overflow-wrap: anywhere;
    }
  `,
})
export class LabRunResultView {
  readonly result = input.required<LabRunView>();

  protected readonly score = computed(() => formatNumber(this.result().best_score, { digits: 3 }));

  protected readonly passedText = computed(() => {
    const reports = this.result().survival_reports;
    const passed = reports.filter((r) => r.passed).length;
    return `${passed} of ${reports.length} tests passed`;
  });

  protected readonly params = computed(() =>
    Object.entries(this.result().best_params).map(([name, value]) => ({
      name,
      label: humanize(name),
      value: paramText(value),
    })),
  );

  protected readonly tests = computed(() =>
    this.result().survival_reports.map((r) => ({
      id: r.test_id,
      label: testLabel(r.test_id),
      passed: r.passed,
      notes: r.notes,
      metrics: Object.entries(r.metrics).map(([key, value]) => ({
        key,
        label: humanize(key),
        value: formatMetric(key, value),
      })),
    })),
  );
}
