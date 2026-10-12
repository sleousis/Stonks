import { ChangeDetectionStrategy, Component, inject, resource } from '@angular/core';
import { RouterLink } from '@angular/router';

import { BriefingsService } from '../../api/briefings.service';
import { BriefingPrefs } from '../../shared/ui/briefing-prefs';
import { NotificationPrefs } from '../../shared/ui/notification-prefs';
import { NotificationSettings } from '../../shared/ui/notification-settings';
import { PageHeader } from '../../shared/ui/page-header';
import { NotificationsTabs } from './notifications-tabs';
import { PushDevices } from './push-devices';
import { TelegramLink } from './telegram-link';

/**
 * Every alert setting in one place (F39): where alerts reach you first
 * (Telegram, the main channel without a broker, then push on this device,
 * your devices and more channels), then what reaches you and when (the
 * alert and channel grid, upcoming events, quiet hours, briefings while
 * they are on, a pointer to price alerts). Settings links here.
 */
@Component({
  selector: 'app-alert-settings-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    RouterLink,
    PageHeader,
    NotificationsTabs,
    NotificationPrefs,
    BriefingPrefs,
    NotificationSettings,
    PushDevices,
    TelegramLink,
  ],
  template: `
    <app-page-header
      title="Alert settings"
      description="What reaches you, where and when. Everything always shows in the Inbox."
    />
    <app-notifications-tabs />

    <div class="layout">
      <section class="group" aria-labelledby="where-group">
        <h2 id="where-group" class="group-title">Where alerts reach you</h2>
        <app-telegram-link />
        <app-notification-settings />
        <app-push-devices />
        <app-notification-prefs [only]="['webhook']" />
      </section>

      <section class="group" aria-labelledby="what-group">
        <h2 id="what-group" class="group-title">What reaches you, and when</h2>
        <app-notification-prefs [only]="['channels', 'events', 'quiet']" />
        @if (briefingsOn()) {
          <app-briefing-prefs />
        }
        <section class="page-section" aria-labelledby="price-alerts-title">
          <header class="section-head">
            <h3 id="price-alerts-title">Price alerts</h3>
          </header>
          <div class="panel-body body">
            <p class="lead">
              Your own rules on a ticker's price. They check each day's closing price after the
              evening data update, not live prices. Where they reach you follows the Price alerts
              row in Which alerts go where.
            </p>
            <a class="btn" routerLink="/notifications/price-alerts">Open price alerts</a>
          </div>
        </section>
      </section>
    </div>
  `,
  styles: `
    @use 'breakpoints' as bp;

    :host {
      display: block;
      min-width: 0;
    }
    app-notifications-tabs {
      margin-bottom: var(--space-6);
    }
    /* Two groups side by side from desktop, 56px apart, sections 40px apart inside. */
    .layout {
      display: grid;
      gap: var(--section-gap) var(--column-gap);
      min-width: 0;
      @include bp.from-desktop {
        grid-template-columns: minmax(0, 2fr) minmax(0, 3fr);
        align-items: start;
      }
    }
    .group {
      display: grid;
      gap: var(--section-gap);
      min-width: 0;
    }
    /* A group title over its sections: larger than a section title, no rule of its own. */
    .group-title {
      color: var(--ink);
      font-size: 1.25rem;
      font-weight: var(--weight-bold);
      letter-spacing: var(--tracking-section);
    }
    .body {
      display: grid;
      justify-items: start;
      gap: var(--space-3);
    }
    .lead {
      max-width: 70ch;
      color: var(--ink-2);
      font-size: var(--text-sm);
    }
  `,
})
export class AlertSettingsPage {
  private readonly briefings = inject(BriefingsService);
  /** Briefings show only while this install has them (FR-16). */
  private readonly briefingPrefs = resource({
    loader: async () => {
      try {
        return await this.briefings.prefs();
      } catch {
        return null;
      }
    },
  });
  protected readonly briefingsOn = () =>
    this.briefingPrefs.hasValue() && this.briefingPrefs.value()?.available === true;
}
