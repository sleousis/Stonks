import { ChangeDetectionStrategy, Component, booleanAttribute, input } from '@angular/core';

import type { MarketSessionsView } from '../../api/models';
import { formatTime } from '../../core/format/format';

const DAY_MS = 86_400_000;
/** After-hours trading runs four hours past the close. */
const POST_MS = 4 * 3_600_000;
/** Labels closer than this (a share of the day) would overlap: the lesser one goes. */
const LABEL_GAP = 0.09;

export interface SpineSegment {
  kind: 'pre' | 'regular' | 'post';
  /** 0..1 across the local day. */
  left: number;
  width: number;
}

export interface SpineMark {
  kind: 'open' | 'close' | 'run';
  at: number;
  label: string;
  /** False when a more important label sits too close. */
  labelled: boolean;
}

export interface Spine {
  segments: SpineSegment[];
  marks: SpineMark[];
  now: number;
}

/** Where `iso` falls in the local day (the console's time zone), 0..1. */
function dayFraction(iso: string): number {
  const [h, m] = formatTime(iso).split(':').map(Number);
  return Number.isFinite(h) && Number.isFinite(m) ? (h * 60 + m) / 1440 : 0;
}

/** One stretch of the day, split in two when it runs past midnight. */
function stretch(kind: SpineSegment['kind'], start: string, ms: number): SpineSegment[] {
  if (ms <= 0) return [];
  const left = dayFraction(start);
  const width = Math.min(1, ms / DAY_MS);
  if (left + width <= 1) return [{ kind, left, width }];
  return [
    { kind, left, width: 1 - left },
    { kind, left: 0, width: left + width - 1 },
  ];
}

/**
 * The trading day across the user's local 24 hours: pre-market and after
 * hours hatched, regular hours solid, where now is, and the next trading
 * run. On a day without a session it draws the next one's hours.
 */
export function daySpine(
  market: MarketSessionsView,
  now: number,
  runAt: string | null = null,
): Spine {
  const day = market.today ?? market.next;
  const pre = Date.parse(day.pre_open);
  const open = Date.parse(day.open);
  const close = Date.parse(day.close);
  const segments = [
    ...stretch('pre', day.pre_open, open - pre),
    ...stretch('regular', day.open, close - open),
    ...stretch('post', day.close, POST_MS),
  ];
  const marks: SpineMark[] = [
    {
      kind: 'open',
      at: dayFraction(day.open),
      label: `Open ${formatTime(day.open)}`,
      labelled: true,
    },
    {
      kind: 'close',
      at: dayFraction(day.close),
      label: `Close ${formatTime(day.close)}`,
      labelled: true,
    },
  ];
  const run = runAt ? Date.parse(runAt) : NaN;
  if (Number.isFinite(run) && run >= now && run - now < DAY_MS) {
    marks.push({
      kind: 'run',
      at: dayFraction(runAt!),
      label: `Run ${formatTime(runAt)}`,
      labelled: true,
    });
  }
  // Keep labels apart: the run first, then the open, then the close.
  const order: SpineMark['kind'][] = ['run', 'open', 'close'];
  const kept: number[] = [];
  for (const kind of order) {
    const mark = marks.find((m) => m.kind === kind);
    if (!mark) continue;
    mark.labelled = kept.every((at) => Math.abs(at - mark.at) >= LABEL_GAP);
    if (mark.labelled) kept.push(mark.at);
  }
  return { segments, marks, now: dayFraction(new Date(now).toISOString()) };
}

/**
 * The day spine in the session rail (desktop only): a 2px track over the
 * local day, hatched pre and post market, solid green regular hours, a Now
 * cursor and the next run as an outlined diamond. Red while trading is
 * halted. Decorative: the session chip and the run chip say the same in
 * words.
 */
@Component({
  selector: 'app-day-spine',
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: { 'aria-hidden': 'true', '[class.halted]': 'halted()' },
  template: `
    @let s = spine();
    <span class="line"></span>
    @for (seg of s.segments; track $index) {
      <span
        class="seg"
        [attr.data-kind]="seg.kind"
        [style.left.%]="seg.left * 100"
        [style.width.%]="seg.width * 100"
      ></span>
    }
    @for (m of s.marks; track m.kind) {
      @if (m.kind === 'run') {
        <span class="diamond" [style.left.%]="m.at * 100"></span>
      }
      @if (m.labelled) {
        <span class="label num" [attr.data-kind]="m.kind" [style.left.%]="m.at * 100">{{
          m.label
        }}</span>
      }
    }
    <span class="now" [style.left.%]="s.now * 100"><span class="now-label num">Now</span></span>
  `,
  styles: `
    :host {
      position: relative;
      display: block;
      flex: 1 1 auto;
      min-width: 160px;
      height: 34px;
      margin: 0 var(--space-4);
    }
    .line,
    .seg {
      position: absolute;
      top: 15px;
      height: 2px;
      border-radius: 1px;
    }
    .line {
      left: 0;
      right: 0;
      background: var(--chrome-rule);
    }
    .seg[data-kind='regular'] {
      top: 14px;
      height: 4px;
      background: color-mix(in srgb, #3fd29a 70%, transparent);
    }
    .seg[data-kind='pre'],
    .seg[data-kind='post'] {
      top: 13px;
      height: 6px;
      background: repeating-linear-gradient(
        45deg,
        color-mix(in srgb, var(--chrome-muted) 35%, transparent) 0 1px,
        transparent 1px 4px
      );
    }
    :host(.halted) .line,
    :host(.halted) .seg[data-kind='regular'] {
      background: var(--halt);
    }
    .now {
      position: absolute;
      top: 9px;
      width: 1px;
      height: 14px;
      background: var(--chrome-ink);
    }
    .now-label {
      position: absolute;
      bottom: 14px;
      left: 0;
      transform: translateX(-50%);
      color: var(--chrome-ink);
      font-size: 10.5px;
    }
    .diamond {
      position: absolute;
      top: 11.5px;
      width: 9px;
      height: 9px;
      border: 1.5px solid var(--chrome-accent);
      background: var(--chrome);
      transform: translateX(-50%) rotate(45deg);
    }
    .label {
      position: absolute;
      top: 21px;
      transform: translateX(-50%);
      color: var(--chrome-muted);
      font-size: 10.5px;
      line-height: 1.2;
    }
    .label[data-kind='run'] {
      color: var(--chrome-accent);
    }
  `,
})
export class DaySpine {
  readonly spine = input.required<Spine>();
  readonly halted = input(false, { transform: booleanAttribute });
}
