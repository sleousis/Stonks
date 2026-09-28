import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
  resource,
} from '@angular/core';
import { RouterLink } from '@angular/router';

import type { ResearchProposalView } from '../../../api/models';
import { ResearchService } from '../../../api/research.service';
import { formatDate, formatDateTime, formatNumber } from '../../../core/format/format';
import { autoRefresh } from '../../../shared/auto-refresh';
import { testLabel } from '../../../shared/lab-results/survival-tests';
import { PageHeader } from '../../../shared/ui/page-header';
import { EmptyState, ErrorState, LoadingState } from '../../../shared/ui/states';
import { StatusPill } from '../../../shared/ui/status-pill';
import { LabNav } from '../lab-nav';
import { className, scoreText } from '../ledger.page';
import { argText, minutesText, universeText, usedOf } from './research-format';

interface TestLine {
  id: string;
  label: string;
  passed: boolean;
  notes: string;
}

/** Survival test lines from a proposal's outcome, when it carries them. */
export function outcomeTests(outcome: Record<string, unknown> | null): TestLine[] {
  const reports = outcome?.['reports'];
  if (!Array.isArray(reports)) return [];
  return reports
    .filter((r): r is Record<string, unknown> => !!r && typeof r === 'object')
    .filter((r) => typeof r['test_id'] === 'string')
    .map((r) => ({
      id: r['test_id'] as string,
      label: testLabel(r['test_id'] as string),
      passed: r['passed'] === true,
      notes: typeof r['notes'] === 'string' ? r['notes'] : '',
    }));
}

/** One research session: its goal, budgets used and every proposal. */
@Component({
  selector: 'app-research-session-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, PageHeader, LabNav, StatusPill, EmptyState, ErrorState, LoadingState],
  template: `
    <app-page-header
      title="Lab"
      description="One research session, its budget and every lab run the assistant proposed."
    />
    <app-lab-nav />

    <a class="back" routerLink="/lab/research">Back to research sessions</a>

    @if (session.error(); as err) {
      <app-error-state
        title="Could not load this research session"
        [error]="err"
        (retry)="session.reload()"
      />
    } @else if (!session.hasValue()) {
      <app-loading-state label="Loading the research session" [rows]="6" />
    } @else {
      @let s = session.value();
      <section class="panel" aria-labelledby="session-title">
        <div class="panel-head">
          <h2 id="session-title">{{ s.goal }}</h2>
          <app-status-pill [status]="s.status" />
        </div>
        <div class="panel-body">
          <ul class="budgets" aria-label="Budget used">
            @for (b of budgets(); track b.label) {
              <li class="budget">
                <span class="b-label">{{ b.label }}</span>
                <span class="b-value num">{{ b.text }}</span>
                <meter
                  min="0"
                  [max]="b.max || 1"
                  [value]="b.used"
                  [attr.aria-label]="b.label + ': ' + b.text"
                ></meter>
              </li>
            }
          </ul>
          <dl class="facts">
            <div>
              <dt>Model</dt>
              <dd>{{ s.model }}, prompt {{ s.prompt_version }}</dd>
            </div>
            <div>
              <dt>Model training cutoff</dt>
              <dd>{{ day(s.model_cutoff) }}. Tests use only data after it.</dd>
            </div>
            <div>
              <dt>Data</dt>
              <dd>{{ universe() }}</dd>
            </div>
            <div>
              <dt>Ran</dt>
              <dd>{{ ranText() }}</dd>
            </div>
            @if (s.stop_reason) {
              <div>
                <dt>Why it stopped</dt>
                <dd>{{ s.stop_reason }}</dd>
              </div>
            }
            @if (s.summary) {
              <div class="wide">
                <dt>Summary</dt>
                <dd>{{ s.summary }}</dd>
              </div>
            }
          </dl>
        </div>
      </section>

      <section class="panel" aria-labelledby="proposals-title">
        <div class="panel-head">
          <h2 id="proposals-title">Proposals</h2>
          <span class="count num">{{ s.proposals.length }}</span>
        </div>
        <div class="panel-body">
          @if (proposals().length === 0) {
            <app-empty-state
              title="No proposals yet"
              message="The assistant's proposals show here as it makes them, each with its hypothesis."
            />
          } @else {
            <ol class="proposals">
              @for (p of proposals(); track p.id) {
                <li class="proposal" [attr.data-status]="p.status">
                  <div class="p-head">
                    <h3>{{ p.seq }}. {{ p.name }}</h3>
                    <app-status-pill [status]="p.status" />
                    @if (p.verdict) {
                      <app-status-pill [status]="p.verdict" />
                    }
                  </div>
                  @if (p.reason) {
                    <p class="reason">{{ p.reason }}</p>
                  }
                  <dl class="facts">
                    <div>
                      <dt>Hypothesis</dt>
                      <dd>{{ p.hypothesis || 'None written' }}</dd>
                    </div>
                    <div>
                      <dt>Premortem</dt>
                      <dd>{{ p.premortem || 'None written' }}</dd>
                    </div>
                    <div>
                      <dt>Test window starts</dt>
                      <dd>{{ p.validation }}</dd>
                    </div>
                    <div>
                      <dt>Trials</dt>
                      <dd class="num">{{ p.trialsText }}</dd>
                    </div>
                    <div>
                      <dt>Best score</dt>
                      <dd class="num">{{ p.score }}</dd>
                    </div>
                    <div>
                      <dt>Compute</dt>
                      <dd class="num">{{ p.compute }}</dd>
                    </div>
                  </dl>
                  @if (p.tests.length) {
                    <ul class="tests" aria-label="Survival tests">
                      @for (t of p.tests; track t.id) {
                        <li>
                          <app-status-pill [status]="t.passed ? 'pass' : 'fail'" />
                          <span>{{ t.label }}</span>
                          @if (t.notes) {
                            <span class="muted">{{ t.notes }}</span>
                          }
                        </li>
                      }
                    </ul>
                  }
                  @if (p.lab_run_id) {
                    <a class="cell-link" [routerLink]="['/lab/ledger', p.lab_run_id]"
                      >Open the lab run in the trial ledger</a
                    >
                  }
                  @if (p.args.length) {
                    <details>
                      <summary>What the assistant proposed ({{ p.args.length }} fields)</summary>
                      <dl class="args">
                        @for (a of p.args; track a.key) {
                          <div>
                            <dt>{{ a.key }}</dt>
                            <dd>{{ a.value }}</dd>
                          </div>
                        }
                      </dl>
                    </details>
                  }
                </li>
              }
            </ol>
          }
        </div>
      </section>
    }
  `,
  styleUrl: '../ledger.page.scss',
  styles: `
    @use 'breakpoints' as bp;
    .panel + .panel {
      margin-top: var(--space-4);
    }
    .panel-head h2 {
      overflow-wrap: anywhere;
    }
    .budgets {
      list-style: none;
      margin: 0 0 var(--space-4);
      padding: 0;
      display: grid;
      gap: var(--space-3);
      grid-template-columns: repeat(auto-fit, minmax(min(100%, 11rem), 1fr));
    }
    .budget {
      display: grid;
      gap: var(--space-1);
      padding: var(--space-3);
      border: 1px solid var(--color-border);
      border-radius: var(--radius-sm);
      background: var(--color-surface-2);
    }
    .b-label {
      font-size: var(--text-xs);
      color: var(--color-ink-2);
    }
    .b-value {
      font-weight: var(--weight-semibold);
    }
    meter {
      width: 100%;
      height: 8px;
    }
    .wide {
      grid-column: 1 / -1;
    }
    .proposals {
      list-style: none;
      margin: 0;
      padding: 0;
      display: grid;
      gap: var(--space-3);
    }
    .proposal {
      display: grid;
      gap: var(--space-2);
      padding: var(--space-3) var(--space-4);
      border: 1px solid var(--color-border);
      border-left: 3px solid var(--color-accent);
      border-radius: var(--radius-sm);
      background: var(--color-surface);
      min-width: 0;
    }
    .proposal[data-status='rejected'],
    .proposal[data-status='failed'] {
      border-left-color: var(--color-loss);
    }
    .p-head {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: var(--space-2);
      h3 {
        margin: 0;
        font-size: var(--text-md);
        overflow-wrap: anywhere;
      }
    }
    .reason {
      margin: 0;
      font-size: var(--text-sm);
      color: var(--color-ink-2);
      overflow-wrap: anywhere;
    }
    .proposal .facts {
      margin: 0;
    }
    .tests {
      list-style: none;
      margin: 0;
      padding: 0;
      display: grid;
      gap: var(--space-1);
      li {
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        gap: var(--space-2);
        font-size: var(--text-sm);
      }
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
      @include bp.phone {
        min-height: var(--touch-min);
      }
    }
    .args {
      margin: var(--space-2) 0 0;
      display: grid;
      gap: var(--space-2);
      dt {
        font-size: var(--text-xs);
        color: var(--color-ink-2);
      }
      dd {
        margin: 0;
        font-family: var(--font-mono);
        font-size: var(--text-xs);
        overflow-wrap: anywhere;
      }
    }
  `,
})
export class ResearchSessionPage {
  private readonly research = inject(ResearchService);

  readonly sessionId = input.required<string>();

  protected readonly session = resource({
    params: () => ({ id: this.sessionId() }),
    loader: ({ params }) => this.research.get(params.id),
  });

  private readonly live = computed(() => {
    if (!this.session.hasValue()) return false;
    const st = this.session.value().status;
    return st === 'queued' || st === 'running';
  });

  constructor() {
    // Reload every minute only while the session still runs.
    autoRefresh(() => (this.live() ? [this.session] : []));
  }

  protected readonly day = (d: string) => formatDate(d);

  protected readonly budgets = computed(() => {
    if (!this.session.hasValue()) return [];
    const s = this.session.value();
    return [
      {
        label: 'Trials used',
        used: s.trials_used,
        max: s.max_trials,
        text: usedOf(s.trials_used, s.max_trials),
      },
      {
        label: 'Proposals made',
        used: s.proposals.length,
        max: s.max_proposals,
        text: usedOf(s.proposals.length, s.max_proposals),
      },
      {
        label: 'Compute used',
        used: s.cpu_seconds_used,
        max: s.max_cpu_seconds,
        text: `${minutesText(s.cpu_seconds_used)} of ${minutesText(s.max_cpu_seconds)}`,
      },
    ];
  });

  protected readonly universe = computed(() =>
    this.session.hasValue()
      ? universeText(this.session.value().universe_id, this.session.value().universe)
      : '',
  );

  protected readonly ranText = computed(() => {
    if (!this.session.hasValue()) return '';
    const s = this.session.value();
    if (!s.started_at) return `Queued ${formatDateTime(s.created_at)}, not started yet`;
    const start = formatDateTime(s.started_at);
    return s.finished_at ? `${start} to ${formatDateTime(s.finished_at)}` : `Since ${start}`;
  });

  protected readonly proposals = computed(() => {
    if (!this.session.hasValue()) return [];
    return [...this.session.value().proposals]
      .sort((a, b) => a.seq - b.seq)
      .map((p) => this.view(p));
  });

  private view(p: ResearchProposalView) {
    return {
      ...p,
      name: className(p.class_path) || 'No strategy named',
      validation: p.validation_start ? formatDate(p.validation_start) : 'n/a',
      trialsText:
        p.budget != null ? usedOf(p.trials, p.budget) : formatNumber(p.trials, { digits: 0 }),
      score: scoreText(p.best_score),
      compute: minutesText(p.cpu_seconds),
      tests: outcomeTests(p.outcome),
      args: Object.entries(p.arguments ?? {}).map(([key, v]) => ({ key, value: argText(v) })),
    };
  }
}
