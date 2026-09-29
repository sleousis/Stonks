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

/** The presets grouped by broker, in the server's order. */
export function groupByBroker(presets: readonly StatementPresetView[]): BrokerExports[] {
  const out = new Map<string, StatementPresetView[]>();
  for (const p of presets) out.set(p.broker, [...(out.get(p.broker) ?? []), p]);
  return [...out].map(([broker, list]) => ({ broker, presets: list }));
}

/**
 * Brokers Stonks cannot connect to, such as DEGIRO: no API it may use, so
 * their accounts come in from the files the trader exports. Each export
 * links to the import page with its preset chosen.
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
          there. Export the files below from the broker and import them. Stonks reads them only, and
          never places an order there.
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
                <h3>{{ b.broker }}</h3>
                <p class="muted">Reads only. Import its exports.</p>
                <ul class="exports">
                  @for (p of b.presets; track p.id) {
                    <li>
                      <a
                        class="export-link"
                        routerLink="/connections/import"
                        [queryParams]="{ preset: p.id }"
                        >{{ p.label }}</a
                      >
                      <span class="muted">{{ p.how_to_export }}</span>
                    </li>
                  }
                </ul>
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
    .brokers {
      display: grid;
      gap: var(--space-4);
      margin-top: var(--space-3);
    }
    .broker h3 {
      margin: 0;
    }
    .exports {
      display: grid;
      gap: var(--space-2);
      margin-top: var(--space-2);
    }
    .exports li {
      display: grid;
      gap: 2px;
      min-width: 0;
      overflow-wrap: anywhere;
    }
    .export-link {
      display: inline-flex;
      align-items: center;
      min-height: 44px;
      font-weight: var(--weight-semibold);
    }
  `,
})
export class BrokerExportsPanel {
  private readonly api = inject(StatementImportsService);

  protected readonly presets = resource({ loader: () => this.api.presets() });
  protected readonly brokers = computed(() =>
    this.presets.hasValue() ? groupByBroker(this.presets.value()) : [],
  );
}
