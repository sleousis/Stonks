import { DOCUMENT } from '@angular/common';
import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  type ElementRef,
  afterRenderEffect,
  computed,
  inject,
  input,
  signal,
  viewChild,
} from '@angular/core';

import { formatMoney, formatNumber, formatPercent } from '../../core/format/format';
import { ThemeService } from '../../core/theme/theme.service';
import {
  CHART_ENGINE,
  type ChartHandle,
  type ChartSeries,
  type ChartTheme,
  type ChartValueFormat,
  type CrosshairReadout,
} from './chart-engine';

/**
 * Time-series chart (equity, drawdown, prices) behind the ChartEngine seam.
 * Resizes with its container, follows the theme, and shows a legend with the
 * values under the crosshair (tap-and-hold on touch screens).
 *
 *   <app-time-series-chart
 *     ariaLabel="Portfolio value and drawdown"
 *     [summary]="chartSummary()"
 *     [series]="[{ id: 'equity', label: 'Value', kind: 'line', color: 'brass', format: 'money', points }]"
 *   />
 */
@Component({
  selector: 'app-time-series-chart',
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: { '[style.--chart-h.px]': 'height()' },
  template: `
    <figure>
      <figcaption class="legend">
        @if (readoutTime(); as t) {
          <span class="time num">{{ t }}</span>
        }
        @for (item of legend(); track item.id) {
          <span class="item">
            <span
              class="swatch"
              [attr.data-color]="item.color"
              [class.dashed]="item.dashed"
              aria-hidden="true"
            ></span>
            {{ item.label }} <strong class="num">{{ item.value }}</strong>
          </span>
        }
      </figcaption>
      @if (failed()) {
        <div class="failed" role="alert">
          @if (summary()) {
            <p class="failed-summary">{{ summary() }}</p>
          }
          <p class="failed-hint">The chart could not load. Reload the page.</p>
        </div>
      }
      <div
        #plot
        class="plot"
        role="img"
        [class.gone]="failed()"
        [attr.aria-label]="ariaLabel()"
      ></div>
      @if (summary() && !failed()) {
        <p class="visually-hidden">{{ summary() }}</p>
      }
    </figure>
  `,
  styles: `
    @use 'breakpoints' as bp;

    :host {
      display: block;
      min-width: 0;
    }
    figure {
      margin: 0;
    }
    .legend {
      display: flex;
      flex-wrap: wrap;
      align-items: baseline;
      gap: var(--space-1) var(--space-4);
      min-height: 22px;
      margin-bottom: var(--space-2);
      font-size: var(--text-sm);
      color: var(--color-ink-2);
    }
    .time {
      color: var(--color-ink-3);
    }
    .item {
      display: inline-flex;
      align-items: center;
      gap: 6px;
    }
    strong {
      color: var(--color-ink);
      font-weight: var(--weight-semibold);
    }
    .swatch {
      width: 10px;
      height: 3px;
      border-radius: 2px;
      background: var(--color-ink-3);
    }
    .swatch[data-color='brass'] {
      background: var(--color-brass);
    }
    .swatch[data-color='primary'] {
      background: var(--color-primary);
    }
    .swatch[data-color='gain'] {
      background: var(--color-gain);
    }
    .swatch[data-color='loss'] {
      background: var(--color-loss);
    }
    .swatch[data-color='info'] {
      background: var(--color-info);
    }
    .swatch[data-color='warn'] {
      background: var(--color-warn);
    }
    .swatch[data-color='ink'] {
      background: var(--color-ink-2);
    }
    .swatch.dashed {
      width: 14px;
      background: repeating-linear-gradient(
        90deg,
        var(--swatch, currentColor) 0 4px,
        transparent 4px 7px
      );
    }
    .swatch.dashed[data-color='primary'] {
      --swatch: var(--color-primary);
    }
    .swatch.dashed[data-color='info'] {
      --swatch: var(--color-info);
    }
    .swatch.dashed[data-color='warn'] {
      --swatch: var(--color-warn);
    }
    .swatch.dashed[data-color='ink'] {
      --swatch: var(--color-ink-2);
    }
    .swatch.dashed[data-color='muted'] {
      --swatch: var(--color-ink-3);
    }
    .failed {
      display: grid;
      gap: var(--space-2);
      padding: var(--space-4);
      border: 1px dashed var(--color-border-strong);
      border-radius: var(--radius-md);
      background: var(--color-surface-2);
      font-size: var(--text-sm);
    }
    .failed-summary {
      color: var(--color-ink);
    }
    .failed-hint {
      color: var(--color-ink-2);
    }
    .plot.gone {
      display: none;
    }
    .plot {
      position: relative;
      height: var(--chart-h, 280px);
    }
    @include bp.phone {
      .plot {
        height: calc(var(--chart-h, 280px) * 0.8);
      }
    }
  `,
})
export class TimeSeriesChart {
  readonly series = input.required<readonly ChartSeries[]>();
  /** Short name of the chart for screen readers. */
  readonly ariaLabel = input.required<string>();
  /** One or two sentences with the takeaway (latest value, change, worst drawdown). */
  readonly summary = input<string | null>(null);
  /** Plot height in px on tablet/desktop; phones use 80%. */
  readonly height = input(280);

  private readonly plot = viewChild.required<ElementRef<HTMLElement>>('plot');
  private readonly loadEngine = inject(CHART_ENGINE);
  private readonly theme = inject(ThemeService);
  private readonly doc = inject(DOCUMENT);
  private readonly handle = signal<ChartHandle | null>(null);
  private readonly readout = signal<CrosshairReadout>({ time: null, values: null });
  /** The engine chunk failed to load (often right after a deploy). */
  protected readonly failed = signal(false);

  protected readonly readoutTime = computed(() => this.readout().time?.slice(0, 16) ?? null);
  protected readonly legend = computed(() => {
    const values = this.readout().values;
    return this.series().map((s) => {
      const last = s.points.at(-1)?.value;
      const value = values ? values.get(s.id) : last;
      return {
        id: s.id,
        label: s.label,
        color: s.color,
        dashed: !!s.dashed,
        value: fmt(value, s.format),
      };
    });
  });

  constructor() {
    let destroyed = false;
    let created: ChartHandle | null = null;
    inject(DestroyRef).onDestroy(() => {
      destroyed = true;
      created?.destroy();
    });

    this.loadEngine()
      .then((engine) => {
        if (destroyed) return;
        created = engine.create(this.plot().nativeElement, this.readTheme());
        created.onCrosshair((r) => this.readout.set(r));
        this.handle.set(created);
      })
      .catch((err: unknown) => {
        console.error('chart engine failed to load', err);
        if (!destroyed) this.failed.set(true);
      });

    afterRenderEffect(() => {
      this.handle()?.setSeries(this.series());
    });
    afterRenderEffect(() => {
      this.theme.resolved();
      this.handle()?.setTheme(this.readTheme());
    });
  }

  private readTheme(): ChartTheme {
    const css = this.doc.defaultView?.getComputedStyle(this.doc.documentElement);
    const v = (name: string, fallback: string) => css?.getPropertyValue(name).trim() || fallback;
    return {
      background: v('--color-surface', '#ffffff'),
      text: v('--color-ink-3', '#5f6b78'),
      grid: v('--chart-grid', '#e3e8ed'),
      border: v('--color-border', '#d3dae1'),
      font: v('--font-sans', 'system-ui'),
      colors: {
        brass: v('--color-brass', '#a26d12'),
        primary: v('--color-primary', '#22477a'),
        gain: v('--color-gain', '#17784a'),
        loss: v('--color-loss', '#b8342a'),
        muted: v('--color-ink-3', '#5f6b78'),
        info: v('--color-info', '#2a5db0'),
        warn: v('--color-warn', '#8f5d00'),
        ink: v('--color-ink-2', '#45515e'),
      },
    };
  }
}

function fmt(value: number | undefined, format: ChartValueFormat | undefined): string {
  if (value === undefined) return '–';
  if (format === 'money') return formatMoney(value);
  if (format === 'percent') return formatPercent(value);
  return formatNumber(value, { digits: 2 });
}
