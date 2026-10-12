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
import { testHint, testLabel, testReason } from './survival-tests';
import {
  type Figure,
  FigureGrid,
  benchmarkFigures,
  benchmarkName,
  pctOrNa,
} from './result-figures';
import { robustnessWords } from './robustness';

export { formatMetric, metricLabel } from '../metrics';

export { testLabel } from './survival-tests';

/**
 * "It passed all 7 robustness tests..." or "It failed 2 of 7 robustness
 * tests (Higher costs, Nearby settings)...". The bold lead before it says
 * Held up or Did not hold up, so the sentence does not say it again.
 */
export function verdictSentence(
  r: Pick<LabRunView, 'verdict' | 'survival_reports' | 'robustness'>,
): string {
  const total = r.survival_reports.length;
  const tests = total === 1 ? 'test' : 'tests';
  if (heldUp(r)) {
    return (
      `It passed ${total === 1 ? 'the' : `all ${total}`} robustness ${tests} ` +
      'on data the search never tuned on. That makes luck less likely, not impossible.'
    );
  }
  const failed = r.survival_reports.filter((t) => !t.passed).map((t) => testLabel(t.test_id));
  if (!failed.length) {
    return 'The run stopped before its robustness tests could pass.';
  }
  return (
    `It failed ${failed.length} of ${total} robustness ${tests} ` +
    `(${failed.join(', ')}). The good backtest may be luck or fitted to the past.`
  );
}

/**
 * "Yearly return vs benchmark" and "Worst drop vs benchmark", from the
 * held-out test's figures and the benchmark over the same days.
 */
export function benchmarkHeadline(r: Pick<LabRunView, 'benchmark' | 'survival_reports'>): Figure[] {
  const b = r.benchmark;
  if (!b) return [];
  const oos = r.survival_reports.find((t) => t.test_id === 'oos')?.metrics ?? {};
  const cagr = oos['cagr_oos'];
  const drop = oos['max_drawdown_oos'];
  const versus = (own: number | null | undefined, bench: number | null | undefined) =>
    own === undefined ? pctOrNa(bench) : `${pctOrNa(own)} vs ${pctOrNa(bench)}`;
  return [
    {
      label: cagr === undefined ? 'Benchmark yearly return' : 'Yearly return vs benchmark',
      value: versus(cagr, b.benchmark_cagr),
      help: 'cagr',
    },
    {
      label: drop === undefined ? 'Benchmark worst drop' : 'Worst drop vs benchmark',
      value: versus(drop, b.benchmark_max_dd),
      help: 'max_drawdown',
    },
  ];
}

/** The run's status from the API's `robustness`, else from its verdict. */
export function heldUp(r: Pick<LabRunView, 'verdict' | 'robustness'>): boolean {
  return r.robustness ? r.robustness === 'survived' : r.verdict === 'pass';
}

function paramText(v: unknown): string {
  if (typeof v === 'number') return formatNumber(v);
  if (typeof v === 'string') return v;
  return JSON.stringify(v);
}

/**
 * One plain verdict, the settings tried, best settings, the benchmark
 * comparison and one pass/fail card per robustness test (why it passed or
 * failed in one sentence, its deciding figures first, the rest folded
 * away).
 */
@Component({
  selector: 'app-lab-run-result',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [FigureGrid, HelpTip, ParamHeatmap, PreflightIssues, RouterLink, StatTile, StatusPill],
  template: `
    @let r = result();
    <p
      class="plain plain-verdict callout"
      [class.failed]="!held()"
      [attr.data-tone]="held() ? 'gain' : 'loss'"
      role="note"
    >
      <strong>{{ status().label }}.</strong> {{ verdictText() }}
      <app-help-tip term="lab_verdict" />
    </p>

    <div class="summary tile-grid">
      <app-stat-tile bare label="Best score" [value]="score()" [detail]="className()" />
      <app-stat-tile
        bare
        label="Settings tried"
        [value]="count(r.n_trials_run)"
        detail="This run"
      />
      <app-stat-tile
        bare
        label="Settings tried in all runs"
        [value]="count(r.n_trials_class)"
        detail="Of this strategy"
      />
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
      Every setting tried is kept in the <a routerLink="/lab/ledger">Lab history</a>.
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
        <app-figure-grid label="Against the benchmark" [figures]="benchHeadline()" />
        <details class="bench-all">
          <summary>All figures</summary>
          <app-figure-grid label="All benchmark figures" [figures]="bench()" />
        </details>
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
            <p class="reason">{{ t.reason }}</p>
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
    /* The verdict: one callout (gain or loss), never a box in a box. */
    .plain {
      overflow-wrap: anywhere;
    }
    .plain > * {
      margin-top: 0;
    }
    .hint {
      font-size: var(--text-sm);
      color: var(--muted);
    }
    .registered,
    .bench-name {
      font-size: var(--text-sm);
      overflow-wrap: anywhere;
    }
    .bench-name {
      margin-bottom: var(--space-2);
    }
    /* Section titles: 15px, ink. */
    h3 {
      display: flex;
      align-items: center;
      gap: var(--space-1);
      margin-bottom: var(--space-2);
      color: var(--ink);
      font-size: var(--text-lead);
      font-weight: var(--weight-bold);
    }
    dl {
      margin: 0;
      display: grid;
      gap: var(--space-3) var(--space-4);
      grid-template-columns: repeat(auto-fill, minmax(min(100%, 7.5rem), 1fr));
    }
    dt {
      margin-bottom: var(--space-1);
      font-size: var(--text-xs);
      color: var(--muted);
    }
    dd {
      margin: 0;
      font-weight: var(--weight-medium);
      overflow-wrap: anywhere;
      white-space: normal;
    }
    dd.na {
      color: var(--muted);
      font-weight: var(--weight-regular);
    }
    .tests {
      list-style: none;
      margin: 0;
      padding: 0;
      display: grid;
      border-top: 2px solid var(--rule-strong);
    }
    /* One row per test with a hairline between; the chip carries pass or fail. */
    .test {
      display: grid;
      gap: var(--space-2);
      padding: var(--space-3) 0;
      border-bottom: 1px solid var(--rule);
      min-width: 0;
    }
    .test:last-child {
      border-bottom: 0;
    }
    .test-head {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: var(--space-2);
    }
    .test-name {
      font-size: var(--text-body);
      font-weight: var(--weight-semibold);
    }
    .reason {
      max-width: 70ch;
      font-size: var(--text-sm);
      overflow-wrap: anywhere;
    }
    .bench-all {
      margin-top: var(--space-2);
    }
    details summary {
      cursor: pointer;
      font-size: var(--text-sm);
      color: var(--ink-2);
      min-height: var(--control-h);
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

  /** The verdict in one plain sentence, naming what failed. */
  protected readonly verdictText = computed(() => verdictSentence(this.result()));
  /** The run's status in the Lab's words: Held up or Did not hold up. */
  protected readonly held = computed(() => heldUp(this.result()));
  protected readonly status = computed(() =>
    robustnessWords(this.held() ? 'survived' : 'did_not_survive'),
  );

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
  /** The two benchmark figures a trader reads first; the rest fold away. */
  protected readonly benchHeadline = computed(() => benchmarkHeadline(this.result()));
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
      reason: testReason(r),
      ...splitMetrics(r.test_id, r.metrics),
    })),
  );

  protected count(v: number | undefined): string {
    return typeof v === 'number' ? formatNumber(v, { digits: 0 }) : 'n/a';
  }
}
