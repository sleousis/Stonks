import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { RouterLink } from '@angular/router';

import type { LabRunView } from '../../api/models';
import { formatNumber } from '../../core/format/format';
import { humanize } from '../../shared/ui/param-form/param-spec';
import { splitMetrics } from '../../shared/metrics';
import { HelpTip } from '../../shared/ui/help-tip';
import { StatTile } from '../../shared/ui/stat-tile';
import { StatusPill } from '../../shared/ui/status-pill';
import { SURVIVAL_TESTS } from './lab-requests';
import { FigureGrid, benchmarkFigures, benchmarkName } from './result-figures';

export { formatMetric, metricLabel } from '../../shared/metrics';

export function testLabel(id: string): string {
  return SURVIVAL_TESTS.find((t) => t.id === id)?.label ?? humanize(id);
}

function paramText(v: unknown): string {
  if (typeof v === 'number') return formatNumber(v);
  if (typeof v === 'string') return v;
  return JSON.stringify(v);
}

/**
 * Verdict, trial counts, best parameters, the benchmark comparison and one
 * pass/fail card per survival test (its deciding figures first, the rest
 * folded away).
 */
@Component({
  selector: 'app-lab-run-result',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [FigureGrid, HelpTip, RouterLink, StatTile, StatusPill],
  template: `
    @let r = result();
    <div class="summary">
      <app-stat-tile
        class="verdict"
        label="Verdict"
        featured
        [value]="r.verdict === 'pass' ? 'Passed' : 'Failed'"
        [detail]="passedText()"
        [detailTone]="r.verdict === 'pass' ? 'gain' : 'loss'"
      />
      <app-stat-tile label="Best score" [value]="score()" [detail]="className()" />
      <app-stat-tile
        label="Trials this run"
        help="trials"
        [value]="count(r.n_trials_run)"
        detail="Parameter sets the tuner tried"
      />
      <app-stat-tile
        label="Trials of this class"
        help="trials"
        [value]="count(r.n_trials_class)"
        detail="Every run so far; the deflated Sharpe counts them"
      />
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

    @if (r.benchmark) {
      <section aria-labelledby="lr-bench-title">
        <h3 id="lr-bench-title">Against the benchmark</h3>
        <p class="muted bench-name">{{ benchName() }}</p>
        <app-figure-grid label="Benchmark figures" [figures]="bench()" />
      </section>
    }

    <section aria-labelledby="survival-title">
      <h3 id="survival-title">Survival tests</h3>
      <ul class="tests">
        @for (t of tests(); track t.id) {
          <li class="test" [class.failed]="!t.passed">
            <div class="test-head">
              <span class="test-name">{{ t.label }} <app-help-tip [term]="t.id" /></span>
              <app-status-pill [status]="t.passed ? 'pass' : 'fail'" />
            </div>
            @if (t.key.length) {
              <dl class="metrics">
                @for (m of t.key; track m.key) {
                  <div>
                    <dt>{{ m.label }} <app-help-tip [term]="m.key" /></dt>
                    <dd class="num" [class.na]="m.value === 'n/a'">{{ m.value }}</dd>
                  </div>
                }
              </dl>
            }
            @if (t.notes) {
              <p class="notes">{{ t.notes }}</p>
            }
            @if (t.rest.length) {
              <details>
                <summary>All figures ({{ t.rest.length }} more)</summary>
                <dl class="metrics rest">
                  @for (m of t.rest; track m.key) {
                    <div>
                      <dt>{{ m.label }} <app-help-tip [term]="m.key" /></dt>
                      <dd class="num" [class.na]="m.value === 'n/a'">{{ m.value }}</dd>
                    </div>
                  }
                </dl>
              </details>
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
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }
    .verdict {
      grid-column: 1 / -1;
    }
    .registered,
    .bench-name {
      font-size: var(--text-sm);
      overflow-wrap: anywhere;
    }
    .bench-name {
      margin-bottom: var(--space-2);
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
    dd.na {
      color: var(--color-ink-3);
      font-weight: var(--weight-regular);
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
      min-width: 0;
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
    details summary {
      cursor: pointer;
      font-size: var(--text-sm);
      color: var(--color-ink-2);
      min-height: 32px;
      display: flex;
      align-items: center;
      @include bp.coarse {
        min-height: var(--touch-min);
      }
    }
    .rest {
      margin-top: var(--space-2);
    }
  `,
})
export class LabRunResultView {
  readonly result = input.required<LabRunView>();

  protected readonly className = computed(() => this.result().class_path.split(':').at(-1) ?? null);
  protected readonly score = computed(() => {
    const s = this.result().best_score;
    return s === null ? 'n/a' : formatNumber(s, { digits: 3 });
  });

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

  protected readonly bench = computed(() => {
    const b = this.result().benchmark;
    return b ? benchmarkFigures(b) : [];
  });
  protected readonly benchName = computed(() => {
    const b = this.result().benchmark;
    return b ? benchmarkName(b) : '';
  });

  protected readonly tests = computed(() =>
    this.result().survival_reports.map((r) => ({
      id: r.test_id,
      label: testLabel(r.test_id),
      passed: r.passed,
      notes: r.notes,
      ...splitMetrics(r.test_id, r.metrics),
    })),
  );

  protected count(v: number | undefined): string {
    return typeof v === 'number' ? formatNumber(v, { digits: 0 }) : 'n/a';
  }
}
