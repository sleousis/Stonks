import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';
import { RouterLink } from '@angular/router';

import type { StrategyStatus } from '../../api/models';
import { LIFECYCLE, STAGES, type Stage, stageOf, stageState } from '../../shared/governance-labels';

interface NextStep {
  text: string;
  /** A link to the tool for the next step. */
  link?: { label: string; commands: string[]; query?: Record<string, string> };
  /** Or the action on this page (Go live). */
  action?: string;
}

/**
 * Where a strategy is on its way to live trading (Draft, Paper, Ready, Live)
 * and a link to the tool for the next step. `golivePassed` is the go-live
 * verdict for a paper strategy (null while unknown).
 *
 *   <app-stage-bar [strategyId]="s.id" [status]="s.status" [golivePassed]="passed()" />
 */
@Component({
  selector: 'app-stage-bar',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink],
  template: `
    <section class="panel stage-bar" aria-labelledby="stage-title">
      <h2 id="stage-title" class="visually-hidden">Stage</h2>
      <ol class="stages" aria-label="Stages to live trading">
        @for (s of stages; track s.id) {
          <li
            [attr.data-state]="stopped() ? 'todo' : state(s.id)"
            [attr.aria-current]="!stopped() && state(s.id) === 'current' ? 'step' : null"
          >
            <span class="dot" aria-hidden="true"></span>
            <span class="stage-text">
              <span class="stage-label">{{ s.label }}</span>
              <span class="stage-detail">{{ s.detail }}</span>
            </span>
          </li>
        }
      </ol>
      <div class="next">
        <p class="next-text">{{ next().text }}</p>
        @if (next().link; as l) {
          <a class="btn" [routerLink]="l.commands" [queryParams]="l.query ?? null">{{ l.label }}</a>
        }
        @if (next().action; as a) {
          <button
            type="button"
            class="btn btn-primary"
            [disabled]="!canGoLive()"
            (click)="goLive.emit()"
          >
            {{ a }}
          </button>
        }
      </div>
    </section>
  `,
  styles: `
    @use 'breakpoints' as bp;

    .stage-bar {
      display: grid;
      gap: var(--space-3);
      padding: var(--space-4);
    }
    .stages {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: var(--space-2);
      margin: 0;
      padding: 0;
      list-style: none;
    }
    .stages li {
      display: flex;
      gap: var(--space-2);
      align-items: flex-start;
      min-width: 0;
      padding-top: var(--space-2);
      border-top: 3px solid var(--color-border);
    }
    .stages li[data-state='done'] {
      border-top-color: var(--color-ink-3);
    }
    .stages li[data-state='current'] {
      border-top-color: var(--color-brass);
    }
    .dot {
      flex: none;
      width: 10px;
      height: 10px;
      margin-top: 4px;
      border: 2px solid var(--color-ink-3);
      border-radius: 50%;
    }
    li[data-state='done'] .dot {
      background: var(--color-ink-3);
    }
    li[data-state='current'] .dot {
      border-color: var(--color-ink);
      background: var(--color-ink);
    }
    .stage-text {
      display: grid;
      min-width: 0;
    }
    .stage-label {
      font-weight: var(--weight-semibold);
    }
    li[data-state='todo'] .stage-label {
      color: var(--color-ink-2);
    }
    .stage-detail {
      font-size: var(--text-xs);
      color: var(--color-ink-3);
    }
    .next {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: var(--space-2) var(--space-3);
    }
    .next-text {
      flex: 1 1 16rem;
      min-width: 0;
      color: var(--color-ink-2);
    }
    @include bp.phone {
      .stages {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }
      .next .btn {
        width: 100%;
      }
    }
  `,
})
export class StageBar {
  readonly strategyId = input.required<string>();
  readonly status = input.required<StrategyStatus>();
  readonly golivePassed = input<boolean | null>(null);
  /** False when the user may not promote (the page shows why). */
  readonly canGoLive = input(true);
  /** The trader chose Go live from the Ready step. */
  readonly goLive = output<void>();

  protected readonly stages = STAGES;
  protected readonly stopped = computed(() => this.status() === 'retired');
  protected readonly current = computed<Stage>(() => stageOf(this.status(), this.golivePassed()));

  protected state(id: Stage) {
    return stageState(id, this.current());
  }

  protected readonly next = computed<NextStep>(() => {
    const id = this.strategyId();
    if (this.stopped()) {
      return {
        text: `Stopped. Use ${LIFECYCLE.paper.label} to run it on paper again.`,
      };
    }
    switch (this.current()) {
      case 'live':
        return {
          text: 'Live: it places orders on every run. Watch what it trades.',
          link: { label: 'See its orders', commands: ['/orders'] },
        };
      case 'ready':
        return {
          text: 'It passed the go-live check. Read the checklist, then go live when you are sure.',
          action: LIFECYCLE.live.label,
        };
      default:
        return {
          text: 'Paper trading. Next: pass the go-live check with enough paper days and trades.',
          link: {
            label: 'Open the go-live check',
            commands: ['/go-live'],
            query: { strategy: id },
          },
        };
    }
  });
}
