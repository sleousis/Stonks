import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  computed,
  inject,
  linkedSignal,
  resource,
  signal,
} from '@angular/core';
import { RouterLink } from '@angular/router';

import type { ResearchSessionView, ResearchStart } from '../../../api/models';
import { ResearchService } from '../../../api/research.service';
import { UniversesService } from '../../../api/universes.service';
import { SessionService } from '../../../core/auth/session.service';
import { ConfirmService } from '../../../core/confirm/confirm.service';
import { formatDateTime } from '../../../core/format/format';
import { ApiError } from '../../../core/http/api-error';
import { type JobHandle, JobsService } from '../../../core/jobs/jobs.service';
import { ToastService } from '../../../core/notify/toast.service';
import { DataTable, TableCell, type TableColumn } from '../../../shared/ui/data-table/data-table';
import { keepLatest } from '../../../shared/ui/data-table/keep-latest';
import { JobProgress } from '../../../shared/ui/job-progress';
import { PageHeader } from '../../../shared/ui/page-header';
import { PermissionNote } from '../../../shared/ui/permission-note';
import { EmptyState, ErrorState, LoadingState } from '../../../shared/ui/states';
import { StatusPill } from '../../../shared/ui/status-pill';
import { parseTickers } from '../lab-requests';
import { LabNav } from '../lab-nav';
import { minutesText, usedOf } from './research-format';

const PAGE_SIZE = 25;
const MIN_GOAL = 10;

export interface ResearchForm {
  goal: string;
  universeId: string;
  tickers: string;
  maxTrials: number | null;
  maxProposals: number | null;
  maxMinutes: number | null;
}

export function defaultResearchForm(): ResearchForm {
  return {
    goal: '',
    universeId: '',
    tickers: '',
    maxTrials: null,
    maxProposals: null,
    maxMinutes: null,
  };
}

export function researchErrors(f: ResearchForm): Partial<Record<string, string>> {
  const e: Partial<Record<string, string>> = {};
  if (f.goal.trim().length < MIN_GOAL)
    e['goal'] = `Say what to look for in ${MIN_GOAL} characters or more.`;
  if (f.goal.length > 4000) e['goal'] = 'At most 4000 characters.';
  if (!f.universeId && parseTickers(f.tickers).length === 0)
    e['tickers'] = 'Enter at least one ticker, or pick a universe.';
  const positive = (v: number | null) => v === null || (Number.isInteger(v) && v >= 1);
  if (!positive(f.maxTrials)) e['maxTrials'] = 'Leave blank or enter a whole number of 1 or more.';
  if (!positive(f.maxProposals))
    e['maxProposals'] = 'Leave blank or enter a whole number of 1 or more.';
  if (f.maxMinutes !== null && !(Number.isFinite(f.maxMinutes) && f.maxMinutes > 0))
    e['maxMinutes'] = 'Leave blank or enter minutes above 0.';
  return e;
}

/** The request body: blank budgets send nothing, so the configured ones apply. */
export function buildResearchStart(f: ResearchForm): ResearchStart {
  const body: ResearchStart = { goal: f.goal.trim() };
  if (f.universeId) body.universe_id = f.universeId;
  else body.universe = parseTickers(f.tickers);
  if (f.maxTrials !== null) body.max_trials = f.maxTrials;
  if (f.maxProposals !== null) body.max_proposals = f.maxProposals;
  if (f.maxMinutes !== null) body.max_cpu_seconds = Math.round(f.maxMinutes * 60);
  return body;
}

/**
 * Research sessions: the assistant proposes lab runs for a goal and the lab
 * runs them under a budget. Lists your sessions and starts a new one.
 */
@Component({
  selector: 'app-research-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    RouterLink,
    PageHeader,
    LabNav,
    DataTable,
    TableCell,
    StatusPill,
    JobProgress,
    PermissionNote,
    EmptyState,
    ErrorState,
    LoadingState,
  ],
  template: `
    <app-page-header
      title="Lab"
      description="Let the assistant propose and run lab trials for a goal, under a budget."
    />
    <app-lab-nav />

    <div class="layout">
      <section class="panel" aria-labelledby="sessions-title">
        <div class="panel-head">
          <h2 id="sessions-title">Research sessions</h2>
          @if (page(); as p) {
            <span class="count num">{{ p.total }} sessions</span>
          }
        </div>
        <div class="panel-body">
          <p class="lead">
            The assistant writes down every proposal with its hypothesis before it runs. Tests only
            count data after the model's training cutoff, because a model may remember older prices.
            A session never starts paper trading and never goes live. You decide that yourself.
          </p>
          @if (sessions.error(); as err) {
            <app-error-state
              title="Could not load the research sessions"
              [error]="err"
              (retry)="sessions.reload()"
            />
          } @else if (page(); as p) {
            @if (p.items.length === 0) {
              <app-empty-state
                title="No research sessions yet"
                message="Start one with the form here, or ask the assistant in the chat to research a goal."
              />
            } @else {
              <app-data-table
                caption="Your research sessions"
                [rows]="p.items"
                [columns]="columns"
                [rowKey]="key"
                [total]="p.total"
                [offset]="p.offset"
                [busy]="sessions.isLoading()"
                [pageSize]="pageSize"
                (pageChange)="offset.set($event.offset)"
              >
                <ng-template appCell="created_at" [appCellOf]="p.items" let-s>
                  <a class="cell-link" [routerLink]="['/lab/research', s.id]">{{
                    when(s.created_at)
                  }}</a>
                </ng-template>
                <ng-template appCell="status" [appCellOf]="p.items" let-s>
                  <app-status-pill [status]="s.status" />
                </ng-template>
              </app-data-table>
            }
          } @else {
            <app-loading-state label="Loading the research sessions" [rows]="5" />
          }
        </div>
      </section>

      <section class="panel" aria-labelledby="start-title">
        <div class="panel-head">
          <h2 id="start-title">Start a session</h2>
        </div>
        <div class="panel-body">
          <form class="stack" novalidate (submit)="$event.preventDefault(); submit()">
            <div class="field">
              <label for="rs-goal">What to look for</label>
              <textarea
                id="rs-goal"
                class="input"
                rows="3"
                aria-describedby="rs-goal-hint"
                [value]="form().goal"
                [attr.aria-invalid]="!!errors()['goal']"
                (input)="patch({ goal: $any($event.target).value })"
              ></textarea>
              @if (errors()['goal']; as e) {
                <span class="error" id="rs-goal-hint">{{ e }}</span>
              } @else {
                <span class="hint" id="rs-goal-hint">
                  For example: a trend rule that holds up on large tech stocks after costs.
                </span>
              }
            </div>

            <div class="field">
              <label for="rs-universe">Run on</label>
              <select
                id="rs-universe"
                class="input"
                (change)="patch({ universeId: $any($event.target).value })"
              >
                <option value="" [selected]="!form().universeId">Tickers I type</option>
                @for (u of universeList(); track u.id) {
                  <option [value]="u.id" [selected]="u.id === form().universeId">
                    The {{ u.name || u.id }} universe
                  </option>
                }
              </select>
            </div>

            @if (!form().universeId) {
              <div class="field">
                <label for="rs-tickers">Tickers</label>
                <input
                  id="rs-tickers"
                  class="input"
                  autocomplete="off"
                  aria-describedby="rs-tickers-hint"
                  [value]="form().tickers"
                  [attr.aria-invalid]="!!errors()['tickers']"
                  (input)="patch({ tickers: $any($event.target).value })"
                />
                @if (errors()['tickers']; as e) {
                  <span class="error" id="rs-tickers-hint">{{ e }}</span>
                } @else {
                  <span class="hint" id="rs-tickers-hint">Separate with commas or spaces.</span>
                }
              </div>
            }

            <fieldset class="budgets">
              <legend>
                Budget <span class="muted">(optional, only lower than the limit)</span>
              </legend>
              <div class="budget-grid">
                <div class="field">
                  <label for="rs-trials">Most trials</label>
                  <input
                    id="rs-trials"
                    class="input num"
                    type="number"
                    inputmode="numeric"
                    min="1"
                    step="1"
                    placeholder="Limit"
                    [attr.aria-invalid]="!!errors()['maxTrials']"
                    [attr.aria-describedby]="errors()['maxTrials'] ? 'rs-trials-err' : null"
                    [value]="form().maxTrials ?? ''"
                    (input)="setNumber('maxTrials', $any($event.target).value)"
                  />
                  @if (errors()['maxTrials']; as e) {
                    <span class="error" id="rs-trials-err">{{ e }}</span>
                  }
                </div>
                <div class="field">
                  <label for="rs-proposals">Most proposals</label>
                  <input
                    id="rs-proposals"
                    class="input num"
                    type="number"
                    inputmode="numeric"
                    min="1"
                    step="1"
                    placeholder="Limit"
                    [attr.aria-invalid]="!!errors()['maxProposals']"
                    [attr.aria-describedby]="errors()['maxProposals'] ? 'rs-proposals-err' : null"
                    [value]="form().maxProposals ?? ''"
                    (input)="setNumber('maxProposals', $any($event.target).value)"
                  />
                  @if (errors()['maxProposals']; as e) {
                    <span class="error" id="rs-proposals-err">{{ e }}</span>
                  }
                </div>
                <div class="field">
                  <label for="rs-minutes">Most compute (minutes)</label>
                  <input
                    id="rs-minutes"
                    class="input num"
                    type="number"
                    inputmode="decimal"
                    min="1"
                    step="1"
                    placeholder="Limit"
                    [attr.aria-invalid]="!!errors()['maxMinutes']"
                    [attr.aria-describedby]="errors()['maxMinutes'] ? 'rs-minutes-err' : null"
                    [value]="form().maxMinutes ?? ''"
                    (input)="setNumber('maxMinutes', $any($event.target).value)"
                  />
                  @if (errors()['maxMinutes']; as e) {
                    <span class="error" id="rs-minutes-err">{{ e }}</span>
                  }
                </div>
              </div>
            </fieldset>

            <div class="actions">
              <button
                type="submit"
                class="btn btn-primary"
                [disabled]="starting() || !canRun()"
                [attr.aria-busy]="starting()"
              >
                {{ starting() ? 'Starting…' : 'Start research' }}
              </button>
              <app-permission-note permission="lab.run" />
            </div>
          </form>

          @if (offNote()) {
            <p class="off-note" role="status">
              Research needs the assistant switched on, with a model and its training cutoff date
              set. Ask your admin to set both.
            </p>
          }

          @if (job(); as h) {
            <div class="job">
              <app-job-progress label="Research session" [handle]="h" />
              @if (startedId(); as id) {
                <a class="cell-link" [routerLink]="['/lab/research', id]">Open the session</a>
              }
            </div>
          }
        </div>
      </section>
    </div>
  `,
  styleUrl: '../ledger.page.scss',
  styles: `
    @use 'breakpoints' as bp;
    .layout {
      display: grid;
      gap: var(--space-4);
      grid-template-columns: minmax(0, 1fr);
      align-items: start;
      @include bp.from-desktop {
        grid-template-columns: minmax(0, 7fr) minmax(0, 5fr);
      }
    }
    .stack {
      display: grid;
      gap: var(--space-4);
    }
    .budgets {
      margin: 0;
      padding: 0;
      border: 0;
      min-width: 0;
      legend {
        margin-bottom: var(--space-2);
        font-weight: var(--weight-semibold);
      }
    }
    .budget-grid {
      display: grid;
      gap: var(--space-3);
      grid-template-columns: repeat(auto-fit, minmax(min(100%, 9rem), 1fr));
    }
    .actions {
      display: grid;
      justify-items: start;
      gap: var(--space-1);
      @include bp.phone {
        .btn {
          width: 100%;
          min-height: var(--touch-min);
        }
      }
    }
    .off-note {
      margin: var(--space-4) 0 0;
      padding: var(--space-3) var(--space-4);
      border-left: 3px solid var(--color-loss);
      border-radius: var(--radius-sm);
      background: var(--color-surface-2);
    }
    .job {
      display: grid;
      gap: var(--space-2);
      margin-top: var(--space-4);
    }
  `,
})
export class ResearchPage {
  private readonly research = inject(ResearchService);
  private readonly universes = inject(UniversesService);
  private readonly session = inject(SessionService);
  private readonly confirm = inject(ConfirmService);
  private readonly toasts = inject(ToastService);
  private readonly jobs = inject(JobsService);
  private readonly destroyRef = inject(DestroyRef);

  protected readonly pageSize = PAGE_SIZE;
  protected readonly key = (s: ResearchSessionView) => s.id;
  protected readonly when = (iso: string) => formatDateTime(iso);
  protected readonly canRun = computed(() => this.session.can('lab.run'));

  protected readonly offset = linkedSignal(() => 0);
  protected readonly sessions = resource({
    params: () => ({ limit: PAGE_SIZE, offset: this.offset() }),
    loader: ({ params }) => this.research.list(params),
  });
  protected readonly page = keepLatest(this.sessions);

  private readonly universeRes = resource({ loader: () => this.universes.list() });
  protected readonly universeList = computed(() =>
    this.universeRes.hasValue() ? this.universeRes.value() : [],
  );

  protected readonly columns: TableColumn<ResearchSessionView>[] = [
    { key: 'created_at', label: 'Started', sortable: false, mobile: 'title' },
    { key: 'goal', label: 'Goal', sortable: false },
    { key: 'status', label: 'Status', sortable: false },
    {
      key: 'trials',
      label: 'Trials used',
      sortable: false,
      help: 'trials',
      value: (s) => usedOf(s.trials_used, s.max_trials),
    },
    {
      key: 'compute',
      label: 'Compute used',
      sortable: false,
      mobile: 'hide',
      value: (s) => `${minutesText(s.cpu_seconds_used)} of ${minutesText(s.max_cpu_seconds)}`,
    },
    { key: 'model', label: 'Model', sortable: false, mobile: 'hide' },
  ];

  protected readonly form = signal<ResearchForm>(defaultResearchForm());
  protected readonly tried = signal(false);
  private readonly allErrors = computed(() => researchErrors(this.form()));
  protected readonly errors = computed(() => (this.tried() ? this.allErrors() : {}));

  protected readonly starting = signal(false);
  protected readonly offNote = signal(false);
  protected readonly job = signal<JobHandle | null>(null);
  protected readonly startedId = signal<string | null>(null);

  protected patch(p: Partial<ResearchForm>): void {
    this.form.update((f) => ({ ...f, ...p }));
  }

  protected setNumber(key: 'maxTrials' | 'maxProposals' | 'maxMinutes', raw: string): void {
    const n = raw.trim() === '' ? null : Number(raw);
    this.patch({ [key]: n === null || Number.isNaN(n) ? null : n });
  }

  async submit(): Promise<void> {
    if (!this.canRun()) return;
    this.tried.set(true);
    if (Object.keys(this.allErrors()).length) return;
    const body = buildResearchStart(this.form());
    const on = body.universe_id
      ? `the ${body.universe_id} universe`
      : `${body.universe?.length ?? 0} tickers`;
    const ok = await this.confirm.confirm({
      title: 'Start a research session?',
      message: `The assistant proposes lab runs on ${on} and the lab runs them in the background, within the budget. Nothing trades.`,
      confirmLabel: 'Start research',
    });
    if (!ok) return;
    this.starting.set(true);
    this.offNote.set(false);
    let jobId: string;
    try {
      const job = await this.research.start(body);
      jobId = job.id;
      const sid = (job.params as Record<string, unknown> | undefined)?.['session_id'];
      this.startedId.set(typeof sid === 'string' ? sid : null);
    } catch (err) {
      // The error interceptor toasts the API's message; say what to do too.
      if (err instanceof ApiError && err.status === 503) this.offNote.set(true);
      return;
    } finally {
      this.starting.set(false);
    }
    this.toasts.success('Started the research session.');
    this.form.set(defaultResearchForm());
    this.tried.set(false);
    this.offset.set(0);
    this.sessions.reload();
    const handle = this.jobs.track(jobId, this.destroyRef);
    this.job.set(handle);
    await handle.finished;
    this.sessions.reload();
  }
}
