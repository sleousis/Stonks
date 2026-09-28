import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { RouterLink } from '@angular/router';

import type { LabRunView } from '../../api/models';
import { formatNumber } from '../../core/format/format';
import { humanize } from '../ui/param-form/param-spec';
import { strategyDisplayName, strategyKindName } from '../strategy-names';
import { splitMetrics } from '../metrics';
import { HelpTip } from '../ui/help-tip';
import { StatTile } from '../ui/stat-tile';
import { StatusPill } from '../ui/status-pill';
import { ParamHeatmap } from './param-heatmap';
import { PreflightIssues } from './preflight-issues';
import { testHint, testLabel } from './survival-tests';
import { FigureGrid, benchmarkFigures, benchmarkName } from './result-figures';

export { formatMetric, metricLabel } from '../metrics';

export { testLabel } from './survival-tests';

/**
 * "It held up: it passed all 7 robustness tests..." or "It did not hold up:
 * it failed 2 of 7 robustness tests (Higher costs, Nearby settings)...".
 */
export function verdictSentence(r: Pick<LabRunView, 'verdict' | 'survival_reports'>): string {
  const total = r.survival_reports.length;
  const tests = total === 1 ? 'test' : 'tests';
  if (r.verdict === 'pass') {
    return (
      `It held up: it passed ${total === 1 ? 'the' : `all ${total}`} robustness ${tests} ` +
      'on data the search never tuned on. That makes luck less likely, not impossible.'
    );
  }
  const failed = r.survival_reports.filter((t) => !t.passed).map((t) => testLabel(t.test_id));
  if (!failed.length) {
    return 'It did not hold up: the run stopped before its robustness tests could pass.';
  }
  return (
    `It did not hold up: it failed ${failed.length} of ${total} robustness ${tests} ` +
    `(${failed.join(', ')}). The good backtest may be luck or fitted to the past.`
  );
}

function paramText(v: unknown): string {
  if (typeof v === 'number') return formatNumber(v);
  if (typeof v === 'string') return v;
  return JSON.stringify(v);
}

/**
 * A plain verdict, trial counts, best settings, the benchmark comparison
 * and one pass/fail card per robustness test (what it guards against, its
 * deciding figures first, the rest folded away). The verdict is about the
 * robustness tests; trials are only the settings the search tried.
 */
@Component({
  selector: 'app-lab-run-result',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [FigureGrid, HelpTip, ParamHeatmap, PreflightIssues, RouterLink, StatTile, StatusPill],
  template: `
    @let r = result();
    <div class="summary">
      <app-stat-tile
        class="verdict"
        label="Robustness verdict"
        help="lab_verdict"
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
        detail="Settings the search tried"
      />
      <app-stat-tile
        label="Trials of this strategy"
        help="trials"
        [value]="count(r.n_trials_class)"
        detail="All runs so far"
      />
    </div>

    <div class="plain" [class.failed]="r.verdict !== 'pass'" role="note">
      <p class="plain-verdict">{{ verdictText() }}</p>
      <p class="muted">
        Trials are the settings the search tried. A trial only says a setting ran. The verdict comes
        from the robustness tests on the best one, so a run can fail with every trial done.
      </p>
    </div>

    @if (r.registered_strategy_id; as id) {
      <p class="registered">
        Put on trial as
        <a [routerLink]="['/strategies', id]">{{ displayName(id) }}</a
        >.
      </p>
    }

    @if (r.ensure_job_id) {
      <p class="data-job">
        Missing prices were fetched first, in a separate data job, before the tuning started.
      </p>
    }
    <p class="ledger-link">
      Every setting tried is kept in the <a routerLink="/lab/ledger">trial ledger</a>.
    </p>

    @if (r.preflight) {
      <app-preflight-issues [preflight]="r.preflight" />
    }

    <section aria-labelledby="best-params-title">
      <h3 id="best-params-title">Best settings</h3>
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
        <p class="muted">It has no settings to tune.</p>
      }
    </section>

    @if (r.benchmark) {
      <section aria-labelledby="lr-bench-title">
        <h3 id="lr-bench-title">Against the benchmark</h3>
        <p class="muted bench-name">{{ benchName() }}</p>
        <app-figure-grid label="Benchmark figures" [figures]="bench()" />
      </section>
    }

    @if (r.heatmap; as h) {
      <section aria-labelledby="lr-heatmap-result-title">
        <h3 id="lr-heatmap-result-title">Parameter heatmap</h3>
        <app-param-heatmap [heatmap]="h" />
      </section>
    }

    <section aria-labelledby="survival-title">
      <h3 id="survival-title">Robustness tests <app-help-tip term="robustness_tests" /></h3>
      <ul class="tests">
        @for (t of tests(); track t.id) {
          <li class="test" [class.failed]="!t.passed">
            <div class="test-head">
              <span class="test-name">{{ t.label }} <app-help-tip [term]="t.id" /></span>
              <app-status-pill [status]="t.passed ? 'pass' : 'fail'" />
            </div>
            @if (t.hint) {
              <p class="hint">{{ t.hint }}</p>
            }
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
          <li class="muted">No robustness tests ran.</li>
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
    .plain {
      display: grid;
      gap: var(--space-1);
      padding: var(--space-3) var(--space-4);
      border: 1px solid var(--color-border);
      border-left: 3px solid var(--color-gain);
      border-radius: var(--radius-sm);
      background: var(--color-surface-2);
      font-size: var(--text-sm);
      overflow-wrap: anywhere;
    }
    .plain.failed {
      border-left-color: var(--color-loss);
    }
    .plain-verdict {
      font-weight: var(--weight-medium);
    }
    .hint {
      font-size: var(--text-xs);
      color: var(--color-ink-3);
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
      grid-template-columns: repeat(auto-fill, minmax(min(100%, 7.5rem), 1fr));
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
  /** The strategy's plain name when the page knows it (else read from its class). */
  readonly strategyName = input<string | null>(null);

  protected readonly displayName = strategyDisplayName;
  protected readonly className = computed(
    () => this.strategyName() ?? strategyKindName(this.result().class_path),
  );
  protected readonly score = computed(() => {
    const s = this.result().best_score;
    return s === null ? 'n/a' : formatNumber(s, { digits: 3 });
  });

  protected readonly passedText = computed(() => {
    const reports = this.result().survival_reports;
    const passed = reports.filter((r) => r.passed).length;
    return `${passed} of ${reports.length} robustness tests passed`;
  });

  /** The verdict in one plain sentence, naming what failed. */
  protected readonly verdictText = computed(() => verdictSentence(this.result()));

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
      hint: testHint(r.test_id),
      passed: r.passed,
      notes: r.notes,
      ...splitMetrics(r.test_id, r.metrics),
    })),
  );

  protected count(v: number | undefined): string {
    return typeof v === 'number' ? formatNumber(v, { digits: 0 }) : 'n/a';
  }
}
