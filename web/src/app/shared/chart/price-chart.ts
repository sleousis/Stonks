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

import { formatMoney, formatNumber } from '../../core/format/format';
import { ThemeService } from '../../core/theme/theme.service';
import {
  CHART_ENGINE,
  type Candle,
  type PriceChartData,
  type PriceChartHandle,
  type PriceReadout,
} from './chart-engine';
import { readChartTheme } from './chart-theme';

/**
 * Candlestick chart of one ticker behind the ChartEngine seam: candles,
 * volume, overlay lines (moving averages) and markers (your fills, strategy
 * signals). The legend reads open, high, low, close and volume at the
 * crosshair (tap and hold on a phone), else of the last bar. The library
 * loads only when a chart first shows.
 *
 *   <app-price-chart ariaLabel="AAPL.US daily candles" [summary]="summary()" [data]="data()" />
 */
@Component({
  selector: 'app-price-chart',
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: { '[style.--chart-h.px]': 'height()' },
  template: `
    <figure>
      <figcaption class="legend">
        @if (bar(); as b) {
          <span class="time num">{{ b.time.slice(0, 10) }}</span>
          <span class="ohlc num">
            <span>O {{ price(b.open) }}</span>
            <span>H {{ price(b.high) }}</span>
            <span>L {{ price(b.low) }}</span>
            <span [class.gain]="b.close >= b.open" [class.loss]="b.close < b.open"
              >C {{ price(b.close) }}</span
            >
            @if (b.volume !== null) {
              <span class="muted">V {{ volume(b.volume) }}</span>
            }
          </span>
        }
        @for (o of overlays(); track o.id) {
          <span class="item">
            <span class="swatch" [attr.data-color]="o.color" aria-hidden="true"></span>
            {{ o.label }} <strong class="num">{{ o.value }}</strong>
          </span>
        }
      </figcaption>
      @if (failed()) {
        <div class="failed" role="alert">
          @if (summary()) {
            <p>{{ summary() }}</p>
          }
          <p class="muted">The chart could not load. Reload the page.</p>
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
    .ohlc {
      display: inline-flex;
      flex-wrap: wrap;
      gap: 0 var(--space-3);
      color: var(--color-ink);
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
    .swatch[data-color='primary'] {
      background: var(--color-primary);
    }
    .swatch[data-color='info'] {
      background: var(--color-info);
    }
    .swatch[data-color='violet'] {
      background: var(--chart-violet);
    }
    .swatch[data-color='ink'] {
      background: var(--color-ink-2);
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
    .plot {
      position: relative;
      height: var(--chart-h, 420px);
    }
    .plot.gone {
      display: none;
    }
    @include bp.phone {
      .plot {
        height: calc(var(--chart-h, 420px) * 0.75);
      }
    }
  `,
})
export class PriceChart {
  readonly data = input.required<PriceChartData>();
  readonly ariaLabel = input.required<string>();
  /** One or two sentences: the last close, the change over the range, the marks. */
  readonly summary = input<string | null>(null);
  /** Plot height in px on tablet and desktop; phones use 75%. */
  readonly height = input(420);

  private readonly plot = viewChild.required<ElementRef<HTMLElement>>('plot');
  private readonly loadEngine = inject(CHART_ENGINE);
  private readonly theme = inject(ThemeService);
  private readonly doc = inject(DOCUMENT);
  private readonly handle = signal<PriceChartHandle | null>(null);
  private readonly readout = signal<PriceReadout>({ time: null, candle: null, overlays: null });
  protected readonly failed = signal(false);

  /** The bar under the crosshair, else the last one. */
  protected readonly bar = computed<Candle | null>(
    () => this.readout().candle ?? this.data().candles.at(-1) ?? null,
  );
  protected readonly overlays = computed(() => {
    const at = this.readout().overlays;
    return this.data().overlays.map((o) => {
      const value = at ? at.get(o.id) : o.points.at(-1)?.value;
      return {
        id: o.id,
        label: o.label,
        color: o.color,
        value: value === undefined ? '–' : formatNumber(value, { digits: 2 }),
      };
    });
  });

  constructor() {
    let destroyed = false;
    let created: PriceChartHandle | null = null;
    inject(DestroyRef).onDestroy(() => {
      destroyed = true;
      created?.destroy();
    });
    this.loadEngine()
      .then((engine) => {
        if (destroyed) return;
        created = engine.createPrice(this.plot().nativeElement, readChartTheme(this.doc));
        created.onCrosshair((r) => this.readout.set(r));
        this.handle.set(created);
      })
      .catch((err: unknown) => {
        console.error('chart engine failed to load', err);
        if (!destroyed) this.failed.set(true);
      });
    afterRenderEffect(() => {
      this.handle()?.setData(this.data());
    });
    afterRenderEffect(() => {
      this.theme.resolved();
      this.handle()?.setTheme(readChartTheme(this.doc));
    });
  }

  protected price(v: number): string {
    return formatMoney(v);
  }

  protected volume(v: number): string {
    return formatNumber(v, { compact: true });
  }
}
