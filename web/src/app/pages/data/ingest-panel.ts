import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  computed,
  inject,
  input,
  output,
  resource,
  signal,
} from '@angular/core';
import { toSignal } from '@angular/core/rxjs-interop';
import { NonNullableFormBuilder, ReactiveFormsModule } from '@angular/forms';

import { IngestService } from '../../api/ingest.service';
import { JobsApiService } from '../../api/jobs-api.service';
import type { IngestResultView } from '../../api/models';
import { SessionService } from '../../core/auth/session.service';
import { ConfirmService } from '../../core/confirm/confirm.service';
import { type JobHandle, JobsService } from '../../core/jobs/jobs.service';
import { ToastService } from '../../core/notify/toast.service';
import { JobProgress, JobResult } from '../../shared/ui/job-progress';
import { PermissionNote } from '../../shared/ui/permission-note';
import { sourceLabel, updateOutcome } from './data-labels';
import {
  EMPTY_INGEST_FORM,
  INGEST_KINDS,
  type IngestFormValue,
  type IngestSource,
  buildIngestRequest,
  describeIngest,
  ingestProblems,
} from './ingest-request';

const FALLBACK_SOURCES: readonly IngestSource[] = ['eodhd'];

/**
 * "Update data": a form, a confirm step, then the job's live progress in
 * <app-job-progress> (Cancel while queued). Needs `operations.run` (admins);
 * others see the button off with the reason. A failed result read after the
 * job succeeds shows an inline error with Try again.
 */
@Component({
  selector: 'app-ingest-panel',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ReactiveFormsModule, JobProgress, PermissionNote],
  styleUrl: './ingest-panel.scss',
  template: `
    <section class="panel" aria-labelledby="ingest-title">
      <div class="panel-head">
        <h2 id="ingest-title">Update data</h2>
      </div>
      <form class="panel-body form-grid form-grid-2" [formGroup]="form" (ngSubmit)="run()">
        <div class="field">
          <label for="ingest-source">Source</label>
          <select id="ingest-source" class="input" formControlName="source">
            @for (s of sourceOptions(); track s.id) {
              <option [value]="s.id">
                {{ sourceName(s.id) }}{{ s.default ? ' (default)' : ''
                }}{{ s.configured ? '' : ' (not configured)' }}
              </option>
            }
          </select>
          @if (sourceNote(); as note) {
            <span class="hint">{{ note }}</span>
          }
        </div>
        <div class="field">
          <label for="ingest-kind">What</label>
          <select id="ingest-kind" class="input" formControlName="kind">
            @for (k of kinds; track k.value) {
              <option [value]="k.value">{{ k.label }}</option>
            }
          </select>
        </div>
        <div class="field wide">
          <label for="ingest-tickers">Tickers</label>
          <textarea
            id="ingest-tickers"
            class="input tickers"
            rows="2"
            formControlName="tickers"
            placeholder="AAPL.US, MSFT.US"
            autocapitalize="characters"
            spellcheck="false"
            aria-describedby="ingest-tickers-hint"
          ></textarea>
          <span id="ingest-tickers-hint" class="hint"
            >Separate with commas, spaces or new lines.</span
          >
        </div>
        @if (kind() === 'prices') {
          <div class="field">
            <label for="ingest-exchange">Or a whole exchange</label>
            <input
              id="ingest-exchange"
              class="input"
              formControlName="exchange"
              placeholder="e.g. US"
              autocapitalize="characters"
              spellcheck="false"
            />
          </div>
        }
        @if (kind() === 'intraday') {
          <div class="field">
            <label for="ingest-interval">Interval</label>
            <select id="ingest-interval" class="input" formControlName="interval">
              <option value="">Choose…</option>
              @for (code of intradayIntervals(); track code) {
                <option [value]="code">{{ code }}</option>
              }
            </select>
          </div>
        }
        <div class="field">
          <label for="ingest-since">Since</label>
          <input id="ingest-since" class="input" type="date" formControlName="since" />
        </div>
        <div class="field">
          <label for="ingest-until">Until</label>
          <input id="ingest-until" class="input" type="date" formControlName="until" />
        </div>

        @if (problems().length) {
          <ul class="problems wide" role="alert">
            @for (p of problems(); track p) {
              <li>{{ p }}</li>
            }
          </ul>
        }

        <div class="actions wide">
          <button
            type="submit"
            class="btn btn-primary"
            [disabled]="busy() || !allowed()"
            [attr.aria-busy]="busy()"
          >
            {{ busy() ? 'Updating…' : 'Update data' }}
          </button>
          <app-permission-note permission="operations.run" />
        </div>
      </form>

      @if (job(); as j) {
        <app-job-progress label="Data update" [handle]="j" [result]="resultRead">
          @if (j.status() === 'queued') {
            <button
              jobActions
              type="button"
              class="btn btn-ghost"
              [disabled]="cancelling()"
              (click)="cancel(j)"
            >
              {{ cancelling() ? 'Cancelling…' : 'Cancel' }}
            </button>
          }
          @if (resultRead.value(); as r) {
            <p class="job-result">{{ outcome(r) }}</p>
          }
        </app-job-progress>
      }
    </section>
  `,
})
export class IngestPanel {
  private readonly ingestApi = inject(IngestService);
  private readonly jobsApi = inject(JobsApiService);
  private readonly jobs = inject(JobsService);
  private readonly confirm = inject(ConfirmService);
  private readonly toasts = inject(ToastService);
  private readonly destroyRef = inject(DestroyRef);
  private readonly session = inject(SessionService);
  /** POST /api/ingest/runs needs operations.run. */
  protected readonly allowed = computed(() => this.session.can('operations.run'));

  readonly intervals = input<readonly { code: string; is_intraday: boolean }[]>([]);
  /** Emits after a data update ends, so the page can refresh coverage and history. */
  readonly finished = output<void>();

  protected readonly kinds = INGEST_KINDS;
  protected readonly form = inject(NonNullableFormBuilder).group({ ...EMPTY_INGEST_FORM });
  protected readonly kind = toSignal(this.form.controls.kind.valueChanges, {
    initialValue: this.form.controls.kind.value,
  });
  private readonly source = toSignal(this.form.controls.source.valueChanges, {
    initialValue: this.form.controls.source.value,
  });

  protected readonly sources = resource({ loader: () => this.ingestApi.sources() });
  protected readonly sourceOptions = computed(() =>
    this.sources.hasValue() && this.sources.value().length
      ? this.sources.value()
      : FALLBACK_SOURCES.map((id) => ({ id, configured: true, default: true, detail: null })),
  );
  protected readonly sourceNote = computed(() => {
    const s = this.sourceOptions().find((o) => o.id === this.source());
    if (!s) return null;
    if (!s.configured) return `Not configured on the server${s.detail ? `: ${s.detail}` : ''}.`;
    return s.detail ?? null;
  });
  protected readonly intradayIntervals = computed(() => {
    const codes = this.intervals()
      .filter((i) => i.is_intraday)
      .map((i) => i.code);
    return codes.length ? codes : ['1m', '5m', '15m', '30m', '1h'];
  });

  protected readonly problems = signal<string[]>([]);
  protected readonly starting = signal(false);
  protected readonly job = signal<JobHandle | null>(null);
  /** The update's result; a failed read offers Try again. */
  protected readonly resultRead = new JobResult<IngestResultView>();
  protected readonly cancelling = signal(false);
  protected readonly busy = computed(() => {
    const j = this.job();
    return this.starting() || (!!j && !j.done());
  });
  protected readonly outcome = updateOutcome;
  protected readonly sourceName = sourceLabel;

  /** Validate, confirm, start the job and follow it to the end. */
  async run(): Promise<void> {
    const value: IngestFormValue = this.form.getRawValue();
    const problems = ingestProblems(value);
    this.problems.set(problems);
    if (problems.length || this.busy() || !this.allowed()) return;

    const request = buildIngestRequest(value);
    const kindLabel = INGEST_KINDS.find((k) => k.value === request.kind)?.label ?? request.kind;
    const ok = await this.confirm.confirm({
      title: `Update ${kindLabel.toLowerCase()}?`,
      message: describeIngest(request),
      confirmLabel: 'Update data',
    });
    if (!ok) return;

    this.starting.set(true);
    this.resultRead.reset();
    try {
      const started = await this.ingestApi.start(request);
      const handle = this.jobs.track(started.id, this.destroyRef);
      this.job.set(handle);
      this.starting.set(false);
      const last = await handle.finished;
      if (last?.status === 'succeeded') {
        const result = await this.resultRead.load(() => this.ingestApi.result(started.id));
        if (result) this.toasts.success(updateOutcome(result), 'Data update finished');
      } else if (last?.status === 'cancelled') {
        this.toasts.info('Cancelled the data update.');
      }
      // A failure shows once, in the job line.
      this.finished.emit();
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.starting.set(false);
    }
  }

  /** Cancel a queued update, after asking. */
  protected async cancel(j: JobHandle): Promise<void> {
    const ok = await this.confirm.confirm({
      title: 'Cancel this data update?',
      message: 'It has not started yet and will not run.',
      confirmLabel: 'Cancel update',
      cancelLabel: 'Keep running',
      tone: 'danger',
    });
    if (!ok) return;
    this.cancelling.set(true);
    try {
      // The job then ends as cancelled, and run() says so.
      await this.jobsApi.cancel(j.jobId);
    } catch {
      // Toasted by the error interceptor.
    } finally {
      this.cancelling.set(false);
    }
  }
}
