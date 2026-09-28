import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { RouterLink } from '@angular/router';

import type { GoLiveReport, StrategyStatus } from '../../api/models';
import { type CheckRow, checkRow } from '../../shared/golive-checks';
import {
  LIFECYCLE,
  STAGES,
  type Stage,
  readyToApprove,
  stageOf,
  stageState,
} from '../../shared/governance-labels';
import { StatusPill } from '../../shared/ui/status-pill';

interface NextStep {
  text: string;
  /** A link to the tool for the next step. */
  link?: { label: string; commands: string[]; query?: Record<string, string> };
}

/**
 * A strategy's status ladder (Draft, On trial, Approved, Retired), the next
 * step, and the go-live check folded underneath with a fix for each failing
 * check (UX-23, UX-28) when a `report` is given. `golivePassed` is the
 * go-live verdict of a strategy on trial (null while unknown): a pass marks
 * it ready to approve. The Approve button itself sits in the page header.
 *
 * `compact` draws the steps alone, without a frame, next step or checks
 * (Studio's ship panel).
 *
 *   <app-stage-bar [strategyId]="s.id" [status]="s.status" [golivePassed]="passed()" [report]="r" />
 *   <app-stage-bar compact [strategyId]="id" status="draft" />
 */
@Component({
  selector: 'app-stage-bar',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, StatusPill],
  host: { '[class.compact]': 'compact()' },
  template: `
    <section [class.panel]="!compact()" class="stage-bar" aria-labelledby="stage-title">
      <h2 id="stage-title" class="visually-hidden">Status</h2>
      <ol class="stages" aria-label="Status ladder">
        @for (s of stages; track s.id) {
          <li
            [attr.data-state]="state(s.id)"
            [attr.aria-current]="state(s.id) === 'current' ? 'step' : null"
          >
            <span class="dot" aria-hidden="true"></span>
            <span class="stage-text">
              <span class="stage-label">{{ s.label }}</span>
              <span class="stage-detail">{{ detailOf(s.id, s.detail) }}</span>
            </span>
          </li>
        }
      </ol>
      @if (!compact()) {
        <div class="next">
          <p class="next-text">{{ next().text }}</p>
          @if (next().link; as l) {
            <a class="btn" [routerLink]="l.commands" [queryParams]="l.query ?? null">{{
              l.label
            }}</a>
          }
        </div>
        @if (checks().length) {
          <details class="checks" [open]="failing().length > 0 && failing().length <= 3">
            <summary>
              <span>Go-live check</span>
              <app-status-pill
                [status]="failing().length ? 'fail' : 'pass'"
                [label]="
                  failing().length
                    ? failing().length + ' of ' + checks().length + ' to fix'
                    : 'All ' + checks().length + ' passed'
                "
              />
            </summary>
            <ul class="check-list" aria-label="Go-live checks">
              @for (c of checks(); track c.name) {
                <li [class.failed]="!c.passed">
                  <app-status-pill [status]="c.passed ? 'pass' : 'fail'" [label]="c.label" />
                  @if (!c.passed) {
                    <span class="check-detail">{{ c.detail }}</span>
                    @if (c.fix; as f) {
                      <span class="fix">
                        {{ f.text }}
                        @if (f.link; as l) {
                          <a [routerLink]="l.commands" [queryParams]="l.query ?? null">{{
                            l.label
                          }}</a>
                        }
                      </span>
                    }
                  }
                </li>
              }
            </ul>
          </details>
        }
      }
    </section>
  `,
  styles: `
    @use 'breakpoints' as bp;

    :host {
      display: block;
      min-width: 0;
    }
    .stage-bar {
      display: grid;
      gap: var(--space-3);
      padding: var(--space-4);
    }
    :host(.compact) .stage-bar {
      padding: 0;
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
      border-top-color: var(--color-accent);
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
    .checks {
      border-top: 1px solid var(--color-border);
      padding-top: var(--space-2);
    }
    .checks summary {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: var(--space-2);
      min-height: var(--touch-min);
      cursor: pointer;
      font-weight: var(--weight-semibold);
    }
    .check-list {
      display: grid;
      gap: var(--space-2);
      margin: var(--space-2) 0 0;
      padding: 0;
      list-style: none;
      font-size: var(--text-sm);
    }
    .check-list li {
      display: grid;
      gap: 2px;
      min-width: 0;
    }
    .check-detail {
      color: var(--color-ink-2);
      overflow-wrap: anywhere;
    }
    .fix a {
      display: inline-flex;
      align-items: center;
      min-height: var(--touch-min);
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
  readonly status = input.required<StrategyStatus | 'draft'>();
  readonly golivePassed = input<boolean | null>(null);
  /** The go-live report, folded under the bar with a fix per failing check. */
  readonly report = input<GoLiveReport | null>(null);
  /** Steps only: no frame, next step or checks. */
  readonly compact = input(false, { transform: (v: boolean | '') => v !== false });

  protected readonly stages = STAGES;
  protected readonly stopped = computed(() => this.status() === 'retired');
  protected readonly current = computed<Stage>(() => stageOf(this.status()));
  protected readonly ready = computed(() => readyToApprove(this.status(), this.golivePassed()));

  protected readonly checks = computed<CheckRow[]>(() => {
    const r = this.report();
    if (!r || this.status() !== 'shadow') return [];
    return r.checks.map((c) => checkRow(c, this.strategyId()));
  });
  protected readonly failing = computed(() => this.checks().filter((c) => !c.passed));

  /** A retired strategy lights only its own step: the ladder does not say how far it got. */
  protected state(id: Stage) {
    if (this.stopped()) return id === 'retired' ? 'current' : 'todo';
    return stageState(id, this.current());
  }

  /** On trial with a passing check says it is ready to approve. */
  protected detailOf(id: Stage, detail: string): string {
    return id === 'trial' && this.ready() ? 'Passed the go-live check: ready to approve' : detail;
  }

  /** The strategy page's Review tab, where the go-live check lives. */
  private review(label: string): NextStep['link'] {
    return { label, commands: ['/strategies', this.strategyId()], query: { tab: 'review' } };
  }

  protected readonly next = computed<NextStep>(() => {
    switch (this.current()) {
      case 'retired':
        return { text: `Retired: it no longer decides. Use ${LIFECYCLE.paper.label} to test it again.` };
      case 'draft':
        return {
          text: `A draft. Use ${LIFECYCLE.paper.label} to have the system test it on real data.`,
        };
      case 'approved':
        return {
          text:
            'Approved: people can follow it. Real money moves only in a portfolio at a ' +
            'real-money stage, never because of this status.',
        };
      default:
        return this.ready()
          ? {
              text: `It passed the go-live check. Read the evidence, then use ${LIFECYCLE.live.label} when you are sure.`,
              link: this.review('Read the evidence'),
            }
          : {
              text:
                'On trial: the system paper-tests it on its own test book every run. Next: pass ' +
                'the go-live check with enough trial days and trades.',
              link: this.review('See the go-live check'),
            };
    }
  });
}
