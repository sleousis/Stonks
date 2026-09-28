import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  computed,
  inject,
  resource,
  signal,
} from '@angular/core';

import { JobsApiService } from '../../api/jobs-api.service';
import { LabService } from '../../api/lab.service';
import type { SweepRequest, SweepResultView } from '../../api/models';
import { SystemService } from '../../api/system.service';
import { UniversesService } from '../../api/universes.service';
import { SessionService } from '../../core/auth/session.service';
import { ConfirmService } from '../../core/confirm/confirm.service';
import { JobsService } from '../../core/jobs/jobs.service';
import { ToastService } from '../../core/notify/toast.service';
import { SweepResult } from '../../shared/lab-results/sweep-result';
import { JobProgress } from '../../shared/ui/job-progress';
import { PageHeader } from '../../shared/ui/page-header';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import { JobFollower } from './job-follower';
import { strategyTitle } from './lab-requests';
import { LabNav } from './lab-nav';
import { SweepFormView } from './sweep-form';

/**
 * Sweep: every catalogued strategy (or a few) through the lab on one
 * basket, then the verdicts side by side, best first.
 */
@Component({
  selector: 'app-sweeps-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    PageHeader,
    LabNav,
    SweepFormView,
    SweepResult,
    JobProgress,
    ErrorState,
    LoadingState,
    EmptyState,
  ],
  template: `
    <app-page-header
      title="Lab"
      description="Run every strategy through the lab on one basket and compare the verdicts."
    />
    <app-lab-nav />

    <div class="lab-layout">
      <section class="panel" aria-labelledby="sweep-form-title">
        <div class="panel-head">
          <h2 id="sweep-form-title">New sweep</h2>
        </div>
        <div class="panel-body">
          <p class="lead">
            Tests many strategies on the same data with the robustness tests you pick, and ranks
            them. Nothing goes on trial, and every setting tried is counted.
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
            <app-sweep-form
              [classes]="classes.value()"
              [intervals]="intervalList()"
              [universes]="universeList()"
              [busy]="starting()"
              (submitted)="start($event)"
            />
          }
        </div>
      </section>

      <section class="panel result-panel" aria-labelledby="sweep-result-title">
        <div class="panel-head">
          <h2 id="sweep-result-title">Result</h2>
        </div>
        <div class="panel-body result-body">
          @if (run.handle(); as h) {
            @if (!h.done() || h.status() !== 'succeeded') {
              <app-job-progress label="Sweep" [handle]="h" />
            }
            @if (!h.done() && h.status() === 'queued' && canRun()) {
              <button
                type="button"
                class="btn btn-danger"
                [disabled]="cancelling()"
                [attr.aria-busy]="cancelling()"
                (click)="cancel()"
              >
                {{ cancelling() ? 'Cancelling…' : 'Cancel sweep' }}
              </button>
            }
            @if (run.error(); as err) {
              <app-error-state
                title="Could not load the sweep result"
                [error]="err"
                (retry)="run.retry()"
              />
            } @else if (run.loading()) {
              <app-loading-state label="Loading the sweep result" [rows]="6" />
            } @else if (run.result(); as r) {
              <app-sweep-result [result]="r" [titles]="titles()" />
            }
          } @else {
            <app-empty-state
              title="No sweep running"
              message="Start a sweep and its progress shows here. Finished sweeps can be opened from the list of recent jobs on the Test a strategy screen."
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
      justify-items: stretch;
    }
    .result-body > .btn {
      justify-self: start;
    }
  `,
})
export class SweepsPage {
  private readonly lab = inject(LabService);
  private readonly system = inject(SystemService);
  private readonly universesApi = inject(UniversesService);
  private readonly jobsApi = inject(JobsApiService);
  private readonly confirm = inject(ConfirmService);
  private readonly toasts = inject(ToastService);
  private readonly session = inject(SessionService);

  protected readonly canRun = computed(() => this.session.can('lab.run'));
  protected readonly classes = resource({ loader: () => this.system.strategyClasses() });
  /** Strategy id to its plain name, for the result rows. */
  protected readonly titles = computed(
    () =>
      new Map(
        (this.classes.hasValue() ? this.classes.value() : []).map((c) => [
          c.name,
          strategyTitle(c),
        ]),
      ),
  );
  private readonly intervals = resource({ loader: () => this.system.intervals() });
  private readonly universes = resource({ loader: () => this.universesApi.list() });
  protected readonly intervalList = computed(() =>
    this.intervals.hasValue() ? this.intervals.value() : [],
  );
  protected readonly universeList = computed(() =>
    this.universes.hasValue() ? this.universes.value() : [],
  );

  protected readonly starting = signal(false);
  protected readonly cancelling = signal(false);
  protected readonly run = new JobFollower<SweepResultView>(
    inject(JobsService),
    inject(DestroyRef),
    (id) => this.lab.sweepResult(id),
  );

  async start(request: SweepRequest): Promise<void> {
    const count = request.strategies?.length;
    const universe = this.universeList().find((u) => u.id === request.universe_id);
    const basket = request.universe_id
      ? `the ${universe?.name || request.universe_id} universe`
      : `${request.universe?.length ?? 0} ticker${request.universe?.length === 1 ? '' : 's'}`;
    const ok = await this.confirm.confirm({
      title: 'Start a sweep?',
      message:
        `${count ? `${count} strateg${count === 1 ? 'y' : 'ies'}` : 'Every strategy'} on ${basket}, ` +
        `${request.start} to ${request.end}, with the ${request.preset} suite. ` +
        'It can take a while and runs in the background.',
      confirmLabel: 'Start sweep',
    });
    if (!ok) return;
    this.starting.set(true);
    let jobId: string;
    try {
      jobId = (await this.lab.startSweep(request)).id;
    } catch {
      return; // toasted by the error interceptor
    } finally {
      this.starting.set(false);
    }
    this.toasts.success('Started a sweep.');
    await this.run.follow(jobId);
  }

  protected async cancel(): Promise<void> {
    const id = this.run.jobId();
    if (!id) return;
    const ok = await this.confirm.confirm({
      title: 'Cancel this sweep?',
      message: 'It has not started yet and will not run.',
      confirmLabel: 'Cancel sweep',
      cancelLabel: 'Keep it',
      tone: 'danger',
    });
    if (!ok) return;
    this.cancelling.set(true);
    try {
      await this.jobsApi.cancel(id);
      this.toasts.success('Cancelled the sweep.');
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.cancelling.set(false);
    }
  }
}
