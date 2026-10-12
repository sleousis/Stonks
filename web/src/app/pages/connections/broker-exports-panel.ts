import { ChangeDetectionStrategy, Component, computed, inject, resource } from '@angular/core';
import { RouterLink } from '@angular/router';

import type { StatementPresetView } from '../../api/models';
import { StatementImportsService } from '../../api/statement-imports.service';
import { ErrorState, LoadingState } from '../../shared/ui/states';

/** One broker and the exports Stonks reads from it. */
export interface BrokerExports {
  broker: string;
  presets: StatementPresetView[];
}

/** What each export holds, and the order to import them: trades first. */
const PRESET_STEPS: Record<string, { rank: number; text: string }> = {
  degiro_transactions: { rank: 0, text: 'Transactions (your trades)' },
  degiro_account: { rank: 1, text: 'Account statement (dividends, fees, cash in and out)' },
  degiro_portfolio: { rank: 2, text: "Portfolio (optional, today's holdings)" },
};

/** The words for one export step. */
export function presetStep(p: StatementPresetView): string {
  return PRESET_STEPS[p.id]?.text ?? p.label;
}

/** The presets grouped by broker, each in the order to import them. */
export function groupByBroker(presets: readonly StatementPresetView[]): BrokerExports[] {
  const out = new Map<string, StatementPresetView[]>();
  for (const p of presets) out.set(p.broker, [...(out.get(p.broker) ?? []), p]);
  const rank = (p: StatementPresetView) => PRESET_STEPS[p.id]?.rank ?? 99;
  return [...out].map(([broker, list]) => ({
    broker,
    presets: [...list].sort((a, b) => rank(a) - rank(b)),
  }));
}

/**
 * Brokers Stonks cannot connect to, such as DEGIRO: no API it may use, so
 * their accounts come in from the files the trader exports, listed in the
 * order to import them, with one button to the import page.
 */
@Component({
  selector: 'app-broker-exports-panel',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, ErrorState, LoadingState],
  template: `
    <section class="page-section" aria-labelledby="exports-title">
      <div class="section-head">
        <h2 id="exports-title">Brokers without a connection</h2>
      </div>
      <p class="intro">
        These brokers offer no connection Stonks may use, so Stonks never asks for your password
        there. Export the files below from the broker and import them. Stonks only reads them.
      </p>
      @if (presets.error(); as err) {
        <app-error-state
          title="Could not load the broker exports"
          [error]="err"
          (retry)="presets.reload()"
        />
      } @else if (!presets.hasValue()) {
        <app-loading-state label="Loading broker exports" [rows]="1" />
      } @else {
        <ul class="brokers">
          @for (b of brokers(); track b.broker) {
            <li class="broker">
              <span class="mark" aria-hidden="true">{{ b.broker.charAt(0) }}</span>
              <div class="broker-text">
                <h3>{{ b.broker }}</h3>
                <p class="muted">Reads only. Export these files, in this order:</p>
                <ol class="exports">
                  @for (p of b.presets; track p.id) {
                    <li>
                      <strong>{{ step(p) }}</strong>
                      <span class="muted">{{ p.how_to_export }}</span>
                    </li>
                  }
                </ol>
              </div>
              @if (b.presets[0]; as first) {
                <a class="btn" routerLink="/connections/import" [queryParams]="{ preset: first.id }"
                  >Import {{ b.broker }} files</a
                >
              }
            </li>
          }
        </ul>
      }
    </section>
  `,
  styles: `
    @use 'breakpoints' as bp;
    :host {
      display: block;
      min-width: 0;
    }
    ul {
      list-style: none;
      margin: 0;
      padding: 0;
    }
    .intro {
      margin: var(--space-4) 0 var(--space-2);
      max-width: 68ch;
      color: var(--ink-2);
      font-size: var(--text-sm);
    }
    .muted {
      color: var(--muted);
      font-size: var(--text-sm);
    }
    .broker {
      display: grid;
      grid-template-columns: auto minmax(0, 1fr) auto;
      align-items: start;
      gap: var(--space-3) var(--space-4);
      padding: var(--space-4) 0;
      border-bottom: 1px solid var(--rule);
    }
    .mark {
      display: grid;
      place-items: center;
      width: 36px;
      height: 36px;
      border-radius: var(--r-sheet);
      background: var(--sunk);
      color: var(--ink-2);
      font-weight: var(--weight-bold);
    }
    .broker-text {
      display: grid;
      gap: 2px;
      min-width: 0;
    }
    .broker h3 {
      font-size: var(--text-body);
      font-weight: var(--weight-semibold);
      letter-spacing: 0;
    }
    /* The steps on a rail, numbered in mono. */
    .exports {
      display: grid;
      gap: var(--space-3);
      max-width: 68ch;
      margin: var(--space-3) 0 0;
      padding: 0;
      list-style: none;
      counter-reset: step;
    }
    .exports li {
      position: relative;
      display: grid;
      gap: 2px;
      min-width: 0;
      padding-left: var(--space-8);
      overflow-wrap: anywhere;
      counter-increment: step;
    }
    .exports li::before {
      content: counter(step);
      position: absolute;
      top: 0;
      left: 0;
      display: grid;
      place-items: center;
      width: 22px;
      height: 22px;
      border: 1px solid var(--rule-control);
      border-radius: var(--r-pill);
      color: var(--ink-2);
      font-family: var(--font-mono);
      font-size: var(--text-2xs);
      font-weight: var(--weight-semibold);
    }
    .exports li strong {
      font-size: var(--text-md);
      font-weight: var(--weight-semibold);
    }
    @include bp.phone {
      .broker {
        grid-template-columns: auto minmax(0, 1fr);
      }
      .broker .btn {
        grid-column: 1 / -1;
        width: 100%;
      }
    }
  `,
})
export class BrokerExportsPanel {
  private readonly api = inject(StatementImportsService);

  protected readonly presets = resource({ loader: () => this.api.presets() });
  protected readonly step = presetStep;
  protected readonly brokers = computed(() =>
    this.presets.hasValue() ? groupByBroker(this.presets.value()) : [],
  );
}
