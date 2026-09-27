import { ChangeDetectionStrategy, Component, inject, resource } from '@angular/core';
import { RouterLink } from '@angular/router';

import { CalendarsService } from '../../api/calendars.service';

/**
 * The upcoming-event alerts (earnings, ex-dividend) on what you hold or
 * watch, and how far ahead each looks. They are sent as Signals, so the
 * Signals row of the alert settings says where they reach you. Shows
 * nothing when the list cannot be read.
 */
@Component({
  selector: 'app-event-alert-kinds',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink],
  template: `
    @if (kinds.hasValue() && kinds.value().length) {
      <section class="events" aria-labelledby="event-alerts-title">
        <h4 id="event-alerts-title">Upcoming events</h4>
        <p class="hint">
          One alert per event on a ticker you hold or watch. They come as Signals, so switch Signals
          above to choose where they reach you.
        </p>
        <ul class="kinds">
          @for (k of kinds.value(); track k.kind) {
            <li>
              <span class="label">{{ k.label }}</span>
              <span class="ahead">{{ ahead(k.default_days_ahead) }}</span>
            </li>
          }
        </ul>
        <a class="link" routerLink="/calendar">Open the calendar</a>
      </section>
    }
  `,
  styles: `
    :host {
      display: block;
    }
    .events {
      display: grid;
      gap: var(--space-2);
    }
    h4 {
      font-size: var(--text-md);
      font-weight: var(--weight-semibold);
    }
    .hint {
      font-size: var(--text-xs);
      color: var(--color-ink-3);
    }
    .kinds {
      display: grid;
      margin: 0;
      padding: 0;
      list-style: none;
    }
    .kinds li {
      display: flex;
      flex-wrap: wrap;
      justify-content: space-between;
      gap: var(--space-1) var(--space-3);
      padding: var(--space-2) 0;
      border-bottom: 1px solid var(--color-border);
      font-size: var(--text-sm);
    }
    .ahead {
      color: var(--color-ink-2);
    }
    .link {
      display: inline-flex;
      align-items: center;
      min-height: var(--touch-min);
      justify-self: start;
    }
  `,
})
export class EventAlertKinds {
  private readonly api = inject(CalendarsService);

  protected readonly kinds = resource({ loader: () => this.api.alertKinds() });

  protected ahead(days: number): string {
    if (days <= 0) return 'Off';
    return days === 1 ? 'A day ahead' : `${days} days ahead`;
  }
}
