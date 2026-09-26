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
import { AuthTokenService } from '../../core/auth/auth-token.service';
import { ConfirmService } from '../../core/confirm/confirm.service';
import { type JobHandle, JobsService } from '../../core/jobs/jobs.service';
import { ToastService } from '../../core/notify/toast.service';
import { StatusPill } from '../../shared/ui/status-pill';
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

/** "Run ingest": a form, a confirm step, then the job's live progress. */
@Component({
  selector: 'app-ingest-panel',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ReactiveFormsModule, StatusPill],
  styleUrl: './ingest-panel.scss',
  template: `
    <section class="panel" aria-labelledby="ingest-title">
      <div class="panel-head">
        <h2 id="ingest-title">Run ingest</h2>
        @if (!hasToken()) {
          <span class="muted count">Needs the API token (Settings)</span>
        }
      </div>
      <form class="panel-body form-grid form-grid-2" [formGroup]="form" (ngSubmit)="run()">
        <div class="field">
          <label for="ingest-source">Source</label>
          <select id="ingest-source" class="input" formControlName="source">
            @for (s of sourceOptions(); track s.id) {
              <option [value]="s.id">
                {{ s.id }}{{ s.default ? ' (default)' : ''
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
            [disabled]="busy()"
            [attr.aria-busy]="busy()"
          >
            {{ busy() ? 'Ingest running…' : 'Run ingest' }}
          </button>
        </div>
      </form>

      @if (job(); as j) {
        <div class="job" aria-live="polite">
          <div class="job-head">
            <app-status-pill [status]="j.status() ?? 'queued'" />
            <span class="job-id muted">{{ j.jobId }}</span>
            @if (j.status() === 'queued') {
              <button type="button" class="btn btn-ghost" (click)="cancel(j)">Cancel</button>
            }
          </div>
          <label class="progress-label" for="ingest-progress">
            {{ j.message() || (j.done() ? 'Finished' : 'Working…') }}
            <span class="num">{{ percent(j.progress()) }}</span>
          </label>
          <progress id="ingest-progress" max="1" [value]="j.progress()"></progress>
          @if (j.error(); as e) {
            <p class="job-error" role="alert">{{ e }}</p>
          }
          @if (result(); as r) {
            <p class="job-result">
              Run #{{ r.run_id }} {{ r.status }}: {{ r.tickers_ok }} ok,
              {{ r.tickers_failed }} failed.
            </p>
          }
        </div>
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
  protected readonly hasToken = inject(AuthTokenService).hasToken;

  readonly intervals = input<readonly { code: string; is_intraday: boolean }[]>([]);
  /** Emits after an ingest job ends, so the page can refresh coverage and history. */
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
  protected readonly result = signal<IngestResultView | null>(null);
  protected readonly busy = computed(() => {
    const j = this.job();
    return this.starting() || (!!j && !j.done());
  });

  protected percent(p: number): string {
    return `${Math.round(p * 100)}%`;
  }

  /** Validate, confirm, start the job and follow it to the end. */
  async run(): Promise<void> {
    const value: IngestFormValue = this.form.getRawValue();
    const problems = ingestProblems(value);
    this.problems.set(problems);
    if (problems.length || this.busy()) return;

    const request = buildIngestRequest(value);
    const kindLabel = INGEST_KINDS.find((k) => k.value === request.kind)?.label ?? request.kind;
    const ok = await this.confirm.confirm({
      title: `Run ${kindLabel.toLowerCase()} ingest?`,
      message: describeIngest(request),
      confirmLabel: 'Run ingest',
    });
    if (!ok) return;

    this.starting.set(true);
    this.result.set(null);
    try {
      const started = await this.ingestApi.start(request);
      const handle = this.jobs.track(started.id, this.destroyRef);
      this.job.set(handle);
      this.starting.set(false);
      const last = await handle.finished;
      if (last?.status === 'succeeded') {
        const result = await this.ingestApi.result(started.id);
        this.result.set(result);
        this.toasts.success(
          `Ingest run #${result.run_id} finished: ${result.tickers_ok} ok, ${result.tickers_failed} failed.`,
          'Ingest finished',
        );
      } else if (last?.status === 'failed') {
        this.toasts.error(last.error ?? 'The ingest job failed.', 'Ingest failed');
      } else if (last?.status === 'cancelled') {
        this.toasts.info('Ingest cancelled.');
      }
      this.finished.emit();
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.starting.set(false);
    }
  }

  protected async cancel(j: JobHandle): Promise<void> {
    try {
      await this.jobsApi.cancel(j.jobId);
    } catch {
      // Toasted by the error interceptor.
    }
  }
}
