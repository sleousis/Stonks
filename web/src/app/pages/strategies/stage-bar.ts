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
    <section class="stage-bar" aria-labelledby="stage-title">
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

    /* A horizontal stepper: nodes on one 2px track, the current node in
       accent, done nodes filled in ink. The next step sits under it as a
       sentence with one action. */
    :host {
      display: block;
      min-width: 0;
    }
    .stage-bar {
      display: grid;
      gap: var(--space-4);
    }
    .stages {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      margin: 0;
      padding: 0;
      list-style: none;
    }
    .stages li {
      position: relative;
      display: grid;
      gap: var(--space-2);
      align-content: start;
      min-width: 0;
      padding-right: var(--space-3);
    }
    /* The track: from this node to the next. */
    .stages li::before {
      content: '';
      position: absolute;
      top: 6px;
      left: 14px;
      right: 0;
      height: 2px;
      background: var(--rule);
    }
    .stages li:last-child::before {
      display: none;
    }
    .stages li[data-state='done']::before {
      background: var(--ink);
    }
    .dot {
      position: relative;
      width: 14px;
      height: 14px;
      border: 2px solid var(--rule-control);
      border-radius: 50%;
      background: var(--bg);
    }
    li[data-state='done'] .dot {
      border-color: var(--ink);
      background: var(--ink);
    }
    li[data-state='current'] .dot {
      border-color: var(--accent);
      background: var(--accent);
      box-shadow: 0 0 0 4px var(--accent-soft);
    }
    .stage-text {
      display: grid;
      gap: 2px;
      min-width: 0;
    }
    .stage-label {
      font-size: var(--text-md);
      font-weight: var(--weight-semibold);
    }
    li[data-state='current'] .stage-label {
      color: var(--accent);
    }
    li[data-state='todo'] .stage-label {
      color: var(--muted);
      font-weight: var(--weight-label);
    }
    .stage-detail {
      max-width: 26ch;
      color: var(--muted);
      font-size: var(--text-xs);
      line-height: 1.4;
    }
    .next {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: var(--space-2) var(--space-4);
      padding-top: var(--space-3);
      border-top: 1px solid var(--rule);
    }
    .next-text {
      flex: 1 1 16rem;
      min-width: 0;
      max-width: 68ch;
      color: var(--ink-2);
      font-size: var(--text-sm);
    }
    .checks {
      border-top: 1px solid var(--rule);
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
      margin: 0 0 var(--space-2);
      padding: 0;
      list-style: none;
      font-size: var(--text-sm);
    }
    .check-list li {
      display: grid;
      gap: 2px;
      min-width: 0;
      padding: var(--space-2) 0;
      border-bottom: 1px solid var(--rule);
    }
    .check-list li:last-child {
      border-bottom: 0;
    }
    .check-detail {
      color: var(--ink-2);
      overflow-wrap: anywhere;
    }
    .fix a {
      display: inline-flex;
      align-items: center;
      min-height: var(--touch-min);
    }
    /* Phones: two by two, each stage keeping its detail line, as before the
       redesign (old stage-bar.ts:219). The track runs only within a row. */
    @include bp.phone {
      .stages {
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: var(--space-3);
      }
      .stages li:nth-child(2n)::before {
        display: none;
      }
      .stage-label {
        font-size: var(--text-sm);
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
        return {
          text: `Retired: it no longer decides. Use ${LIFECYCLE.paper.label} to test it again.`,
        };
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
