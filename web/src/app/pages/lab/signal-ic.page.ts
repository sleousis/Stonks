import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  computed,
  inject,
  resource,
  signal,
} from '@angular/core';

import type { SignalIcRequest, SignalIcView } from '../../api/models';
import { SignalsService } from '../../api/signals.service';
import { SystemService } from '../../api/system.service';
import { ConfirmService } from '../../core/confirm/confirm.service';
import { JobsService } from '../../core/jobs/jobs.service';
import { ToastService } from '../../core/notify/toast.service';
import { SignalIcResult } from '../../shared/lab-results/signal-ic-result';
import { JobProgress } from '../../shared/ui/job-progress';
import { PageHeader } from '../../shared/ui/page-header';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import { JobFollower } from './job-follower';
import { LabNav } from './lab-nav';
import { SignalIcFormView } from './signal-ic-form';

/** Signal IC: how well a strategy's scores ranked the moves that came next. */
@Component({
  selector: 'app-signal-ic-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    PageHeader,
    LabNav,
    SignalIcFormView,
    SignalIcResult,
    JobProgress,
    ErrorState,
    LoadingState,
    EmptyState,
  ],
  template: `
    <app-page-header
      title="Lab"
      description="Check whether a strategy's scores pick the tickers that go on to do best."
    />
    <app-lab-nav />

    <div class="lab-layout">
      <section class="panel" aria-labelledby="ic-form-title">
        <div class="panel-head">
          <h2 id="ic-form-title">Measure a signal</h2>
        </div>
        <div class="panel-body">
          <p class="lead">
            The strategy scores every ticker on each date. We then compare its ranking with the
            moves that followed. Research only: nothing trades and nothing is registered.
          </p>
          @if (classes.error(); as err) {
            <app-error-state
              title="Could not load the strategies"
              [error]="err"
              (retry)="classes.reload()"
            />
          } @else if (!classes.hasValue()) {
            <app-loading-state label="Loading strategies" [rows]="4" />
          } @else {
            <app-signal-ic-form
              [classes]="classes.value()"
              [intervals]="intervalList()"
              [busy]="starting()"
              (submitted)="start($event)"
            />
          }
        </div>
      </section>

      <section class="panel result-panel" aria-labelledby="ic-result-title">
        <div class="panel-head">
          <h2 id="ic-result-title">Result</h2>
        </div>
        <div class="panel-body result-body">
          @if (run.handle(); as h) {
            @if (!h.done() || h.status() !== 'succeeded') {
              <app-job-progress label="Signal IC" [handle]="h" />
            }
            @if (run.error(); as err) {
              <app-error-state
                title="Could not load the signal result"
                [error]="err"
                (retry)="run.retry()"
              />
            } @else if (run.loading()) {
              <app-loading-state label="Loading the signal result" [rows]="5" />
            } @else if (run.result(); as r) {
              <app-signal-ic-result [result]="r" />
            }
          } @else {
            <app-empty-state
              title="Nothing measured yet"
              message="Pick a strategy and a few tickers, then measure. The IC for each look-ahead shows here."
            />
          }
        </div>
      </section>
    </div>
  `,
  styleUrls: ['./lab.page.scss'],
  styles: `
    .result-body {
      display: grid;
      gap: var(--space-4);
    }
  `,
})
export class SignalIcPage {
  private readonly signals = inject(SignalsService);
  private readonly system = inject(SystemService);
  private readonly confirm = inject(ConfirmService);
  private readonly toasts = inject(ToastService);

  protected readonly classes = resource({ loader: () => this.system.strategyClasses() });
  private readonly intervals = resource({ loader: () => this.system.intervals() });
  protected readonly intervalList = computed(() =>
    this.intervals.hasValue() ? this.intervals.value() : [],
  );

  protected readonly starting = signal(false);
  protected readonly run = new JobFollower<SignalIcView>(
    inject(JobsService),
    inject(DestroyRef),
    (id) => this.signals.signalIcResult(id),
  );

  async start(request: SignalIcRequest): Promise<void> {
    const name = request.strategy.class_path?.split(':').at(-1) ?? 'the strategy';
    const n = request.universe.length;
    const ok = await this.confirm.confirm({
      title: `Measure the signal of ${name}?`,
      message: `${n} ticker${n === 1 ? '' : 's'}, ${request.start} to ${request.end}. It runs in the background.`,
      confirmLabel: 'Measure signal',
    });
    if (!ok) return;
    this.starting.set(true);
    let jobId: string;
    try {
      jobId = (await this.signals.startSignalIc(request)).id;
    } catch {
      return; // toasted by the error interceptor
    } finally {
      this.starting.set(false);
    }
    this.toasts.success(`Started measuring the signal of ${name}.`);
    await this.run.follow(jobId);
  }
}
