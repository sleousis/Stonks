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
    <section class="panel" aria-labelledby="exports-title">
      <div class="panel-head">
        <h2 id="exports-title">Brokers without a connection</h2>
      </div>
      <div class="panel-body">
        <p class="muted">
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
              <li class="broker well">
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
                @if (b.presets[0]; as first) {
                  <a
                    class="btn"
                    routerLink="/connections/import"
                    [queryParams]="{ preset: first.id }"
                    >Import {{ b.broker }} files</a
                  >
                }
              </li>
            }
          </ul>
        }
      </div>
    </section>
  `,
  styles: `
    :host {
      display: block;
      min-width: 0;
    }
    ul {
      list-style: none;
      margin: 0;
      padding: 0;
    }
    .muted {
      color: var(--color-ink-2);
      font-size: var(--text-sm);
    }
    p {
      max-width: 70ch;
    }
    .brokers {
      display: grid;
      gap: var(--space-3);
      margin-top: var(--space-3);
    }
    .broker {
      display: grid;
      justify-items: start;
      gap: var(--space-1);
      padding: var(--space-4);
    }
    .broker h3 {
      margin: 0;
      font-size: var(--text-panel);
      font-weight: var(--weight-semibold);
    }
    .exports {
      display: grid;
      gap: var(--space-2);
      max-width: 70ch;
      margin: var(--space-2) 0 var(--space-3);
      padding-left: var(--space-5);
      list-style: decimal;
    }
    .exports ::marker {
      font-family: var(--font-mono);
      color: var(--color-ink-3);
    }
    .btn {
      min-height: var(--touch-min);
    }
    @media (max-width: 767.98px) {
      .broker {
        padding: var(--space-3);
      }
      .btn {
        width: 100%;
      }
    }
    .exports li {
      display: list-item;
      min-width: 0;
      overflow-wrap: anywhere;
    }
    .exports .muted {
      display: block;
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
