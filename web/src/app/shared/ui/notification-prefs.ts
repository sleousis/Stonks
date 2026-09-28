import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  linkedSignal,
  resource,
  signal,
} from '@angular/core';

import type { PreferenceItem, PreferencesView } from '../../api/models';
import { NotificationsService } from '../../api/notifications.service';
import { SessionService } from '../../core/auth/session.service';
import { ConfirmService } from '../../core/confirm/confirm.service';
import { ToastService } from '../../core/notify/toast.service';
import { ApiError } from '../../core/http/api-error';
import { PermissionNote } from './permission-note';
import { ErrorState, LoadingState } from './states';

type Category = PreferenceItem['category'];

const CATEGORIES: readonly { value: Category; label: string }[] = [
  { value: 'signal', label: 'Signals' },
  { value: 'price_alert', label: 'Price alerts' },
  { value: 'event_alert', label: 'Upcoming events' },
  { value: 'screen_alert', label: 'Screen alerts' },
  { value: 'order', label: 'Orders and fills' },
  { value: 'risk', label: 'Risk alerts' },
  { value: 'system', label: 'System' },
];

const CHANNEL_LABELS: Record<string, string> = {
  webpush: 'Push',
  email: 'Email',
  webhook: 'Webhook',
  telegram: 'Telegram',
};

/** The in-app feed always gets everything; it is not a switch. */
const ALWAYS_ON = 'inapp';

/**
 * Which alerts go where (push, email, webhook) and quiet hours. A switch
 * with no saved choice shows as on: the server's channel default applies
 * until the trader picks.
 */
@Component({
  selector: 'app-notification-prefs',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [LoadingState, ErrorState, PermissionNote],
  template: `
    <section class="panel" aria-labelledby="prefs-title">
      <div class="panel-head">
        <h3 id="prefs-title">Alert settings</h3>
      </div>
      @if (prefs.error(); as err) {
        <app-error-state
          title="Could not load alert settings"
          [error]="err"
          (retry)="prefs.reload()"
        />
      } @else if (!prefs.hasValue()) {
        <app-loading-state label="Loading alert settings" [rows]="4" />
      } @else {
        <div class="panel-body body">
          <p class="lead">Everything shows in the app. Choose what else reaches you.</p>
          <div class="test-row">
            <button
              type="button"
              class="btn"
              [disabled]="testing()"
              [attr.aria-busy]="testing()"
              (click)="sendTest()"
            >
              {{ testing() ? 'Sending…' : 'Send a test notification' }}
            </button>
            <span class="hint">Check that alerts reach your phone and your other channels.</span>
          </div>
          <app-permission-note permission="notifications.manage" />
          @if (channels().length === 0) {
            <p class="note">This server has no push, email or webhook delivery set up yet.</p>
          } @else {
            <div class="grid-scroll">
              <table class="grid">
                <caption class="visually-hidden">
                  Alert types by channel
                </caption>
                <thead>
                  <tr>
                    <th scope="col">Alert</th>
                    @for (c of channels(); track c) {
                      <th scope="col">{{ channelLabel(c) }}</th>
                    }
                  </tr>
                </thead>
                <tbody>
                  @for (cat of categories; track cat.value) {
                    <tr>
                      <th scope="row">{{ cat.label }}</th>
                      @for (c of channels(); track c) {
                        <td>
                          <label class="cell">
                            <input
                              type="checkbox"
                              [checked]="isOn(cat.value, c)"
                              [disabled]="locked()"
                              (change)="setPref(cat.value, c, $event)"
                            />
                            <span class="visually-hidden"
                              >{{ cat.label }} by {{ channelLabel(c) }}</span
                            >
                          </label>
                        </td>
                      }
                    </tr>
                  }
                </tbody>
              </table>
            </div>
          }

          @if (eventAlerts().length) {
            <fieldset class="quiet">
              <legend>Upcoming events</legend>
              <p class="hint">
                Alerts before events on what you hold or watch. Turn a kind off and you get none of
                it, not even in the app.
              </p>
              <div class="kinds">
                @for (e of eventAlerts(); track e.topic) {
                  <label class="kind">
                    <input
                      type="checkbox"
                      [checked]="e.enabled"
                      [disabled]="locked()"
                      (change)="setEventAlert(e.topic, $event)"
                    />
                    <span>{{ e.label }}</span>
                  </label>
                }
              </div>
            </fieldset>
          }

          @if (economic(); as econ) {
            <fieldset class="quiet" aria-describedby="econ-hint">
              <legend>Economic releases</legend>
              <p id="econ-hint" class="hint">
                An alert the day before each release, such as inflation, jobs or a rate decision.
                @if (econ.default_countries) {
                  The countries follow the currencies of your portfolios until you pick your own.
                }
                @if (!economicOn()) {
                  Turn on Economic releases coming up to get them.
                }
              </p>
              <div class="field">
                <label for="econ-importance">Importance</label>
                <select
                  id="econ-importance"
                  class="input"
                  [disabled]="locked()"
                  (change)="setImportance($event)"
                >
                  @for (o of econ.importance_options ?? []; track o.value) {
                    <option [value]="o.value" [selected]="o.value === econ.min_importance">
                      {{ o.label }}
                    </option>
                  }
                </select>
              </div>
              <fieldset class="quiet">
                <legend class="sub">Countries</legend>
                <div class="countries">
                  @for (c of countryChoices(); track c.value) {
                    <label class="kind">
                      <input
                        type="checkbox"
                        [checked]="c.chosen"
                        [disabled]="locked()"
                        (change)="setCountry(c.value, $event)"
                      />
                      <span>{{ c.label }}</span>
                    </label>
                  }
                </div>
              </fieldset>
              @if (countryError(); as err) {
                <p class="error" role="alert">{{ err }}</p>
              }
              @if (!econ.default_countries) {
                <div class="actions">
                  <button
                    type="button"
                    class="btn btn-ghost"
                    [disabled]="locked()"
                    (click)="followPortfolios()"
                  >
                    Follow my portfolio currencies
                  </button>
                </div>
              }
            </fieldset>
          }

          <fieldset class="quiet">
            <legend>Quiet hours</legend>
            <p class="hint">
              Signals, fills, price alerts and upcoming events wait for a morning summary. Risk
              alerts always come through. Times are in {{ view()?.timezone }}.
            </p>
            <div class="times">
              <div class="field">
                <label for="quiet-start">From</label>
                <input
                  id="quiet-start"
                  class="input"
                  type="time"
                  [disabled]="!canManage()"
                  [value]="quietStart()"
                  (input)="quietStart.set($any($event.target).value)"
                />
              </div>
              <div class="field">
                <label for="quiet-end">Until</label>
                <input
                  id="quiet-end"
                  class="input"
                  type="time"
                  [disabled]="!canManage()"
                  [value]="quietEnd()"
                  (input)="quietEnd.set($any($event.target).value)"
                />
              </div>
            </div>
            @if (quietError(); as err) {
              <p class="error" role="alert">{{ err }}</p>
            }
            <div class="actions">
              <button type="button" class="btn" [disabled]="locked()" (click)="saveQuiet()">
                Save quiet hours
              </button>
              @if (view()?.quiet_start) {
                <button
                  type="button"
                  class="btn btn-ghost"
                  [disabled]="locked()"
                  (click)="clearQuiet()"
                >
                  Turn off
                </button>
              }
            </div>
          </fieldset>

          @if (hasWebhook()) {
            <fieldset class="quiet">
              <legend>Your webhook</legend>
              <p class="hint">
                Alerts you send to Webhook above are posted to this address. It must start with
                https. For your safety the full address is never shown again after you save it.
              </p>
              @if (view()?.webhook; as current) {
                <p class="current">
                  Sending to <span class="num">{{ current }}</span>
                </p>
              } @else {
                <p class="current muted">No webhook set.</p>
              }
              <div class="field">
                <label for="webhook-url">{{ view()?.webhook ? 'New address' : 'Address' }}</label>
                <input
                  id="webhook-url"
                  class="input"
                  type="url"
                  inputmode="url"
                  autocomplete="off"
                  spellcheck="false"
                  placeholder="https://"
                  [disabled]="!canManage()"
                  [value]="webhookUrl()"
                  (input)="webhookUrl.set($any($event.target).value)"
                />
              </div>
              @if (webhookError(); as err) {
                <p class="error" role="alert">{{ err }}</p>
              }
              <div class="actions">
                <button type="button" class="btn" [disabled]="locked()" (click)="saveWebhook()">
                  Save webhook
                </button>
                @if (view()?.webhook) {
                  <button
                    type="button"
                    class="btn btn-ghost"
                    [disabled]="locked()"
                    (click)="removeWebhook()"
                  >
                    Remove webhook
                  </button>
                }
              </div>
            </fieldset>
          }
        </div>
      }
    </section>
  `,
  styles: `
    :host {
      display: block;
      min-width: 0;
    }
    .body {
      display: grid;
      grid-template-columns: minmax(0, 1fr);
      gap: var(--space-4);
    }
    .test-row {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: var(--space-2) var(--space-3);
    }
    .lead {
      color: var(--color-ink-2);
    }
    .note {
      padding: var(--space-2) var(--space-3);
      border-left: 3px solid var(--color-info);
      border-radius: var(--radius-sm);
      background: var(--color-info-soft);
      font-size: var(--text-sm);
    }
    /* Four or more channels are wider than a phone: the table scrolls in its own box. */
    .grid-scroll {
      position: relative; /* keeps the hidden cell labels inside the scroll box */
      max-width: 100%;
      overflow-x: auto;
    }
    .grid {
      width: 100%;
      border-collapse: collapse;
      font-size: var(--text-sm);
    }
    .grid th,
    .grid td {
      padding: var(--space-1) var(--space-2);
      border-bottom: 1px solid var(--color-border);
      text-align: center;
    }
    .grid th[scope='row'],
    .grid thead th:first-child {
      text-align: left;
      font-weight: var(--weight-medium);
    }
    .grid thead th {
      color: var(--color-ink-2);
      font-weight: var(--weight-semibold);
    }
    .cell {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-width: var(--touch-min);
      min-height: var(--touch-min);
      cursor: pointer;
    }
    .cell input {
      width: 18px;
      height: 18px;
      accent-color: var(--color-primary);
    }
    .quiet {
      display: grid;
      gap: var(--space-3);
      margin: 0;
      padding: 0;
      border: 0;
    }
    .quiet legend {
      margin-bottom: var(--space-1);
      font-weight: var(--weight-semibold);
    }
    .hint {
      font-size: var(--text-xs);
      color: var(--color-ink-3);
    }
    .kinds {
      display: grid;
      gap: var(--space-1);
    }
    .quiet legend.sub {
      font-size: var(--text-sm);
      font-weight: var(--weight-medium);
    }
    .countries {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(min(100%, 11rem), 1fr));
      gap: 0 var(--space-3);
    }
    .field select {
      max-width: 20rem;
    }
    .kind {
      display: inline-flex;
      align-items: center;
      gap: var(--space-2);
      min-height: var(--touch-min);
      font-size: var(--text-sm);
      cursor: pointer;
    }
    .kind input {
      width: 18px;
      height: 18px;
      accent-color: var(--color-primary);
    }
    .times {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: var(--space-3);
      max-width: 22rem;
    }
    .error {
      font-size: var(--text-sm);
      color: var(--color-loss);
    }
    .current {
      font-size: var(--text-sm);
      overflow-wrap: anywhere;
    }
    .actions {
      display: flex;
      flex-wrap: wrap;
      gap: var(--space-2);
    }
  `,
})
export class NotificationPrefs {
  private readonly api = inject(NotificationsService);
  private readonly toasts = inject(ToastService);
  private readonly confirm = inject(ConfirmService);
  private readonly session = inject(SessionService);

  protected readonly categories = CATEGORIES;
  protected readonly prefs = resource({ loader: () => this.api.preferences() });
  /** The settings as shown, replaced by each save's response. */
  protected readonly view = linkedSignal<PreferencesView | null>(() =>
    this.prefs.hasValue() ? this.prefs.value() : null,
  );
  protected readonly channels = computed(
    () => this.view()?.channels.filter((c) => c !== ALWAYS_ON) ?? [],
  );
  /** One switch per kind of upcoming-event alert (earnings, dividends, economic). */
  protected readonly eventAlerts = computed(() => this.view()?.event_alerts ?? []);
  /** Countries and importance threshold of economic release alerts. */
  protected readonly economic = computed(() => this.view()?.economic_alerts ?? null);
  protected readonly economicOn = computed(
    () => this.eventAlerts().find((e) => e.topic === 'economic')?.enabled ?? true,
  );
  /** The offered countries, plus any chosen code the list does not name. */
  protected readonly countryChoices = computed(() => {
    const econ = this.economic();
    if (!econ) return [];
    const chosen = new Set(econ.countries);
    const offered = (econ.country_options ?? []).map((o) => ({
      value: o.value,
      label: o.label,
      chosen: chosen.has(o.value),
    }));
    const known = new Set(offered.map((o) => o.value));
    const extra = econ.countries
      .filter((c) => !known.has(c))
      .map((c) => ({ value: c, label: c, chosen: true }));
    return [...offered, ...extra];
  });
  protected readonly countryError = signal<string | null>(null);
  protected readonly quietStart = linkedSignal(() => this.view()?.quiet_start ?? '');
  protected readonly quietEnd = linkedSignal(() => this.view()?.quiet_end ?? '');
  protected readonly quietError = signal<string | null>(null);
  protected readonly saving = signal(false);
  protected readonly canManage = computed(() => this.session.can('notifications.manage'));
  /** Controls are off while a save runs, or for a user who may not change settings. */
  protected readonly locked = computed(() => this.saving() || !this.canManage());
  protected readonly hasWebhook = computed(
    () => this.view()?.channels.includes('webhook') ?? false,
  );
  /** Write-only: never filled from the server, cleared after each save. */
  protected readonly webhookUrl = signal('');
  protected readonly webhookError = signal<string | null>(null);

  protected channelLabel(channel: string): string {
    return CHANNEL_LABELS[channel] ?? channel;
  }

  protected isOn(category: Category, channel: string): boolean {
    const found = this.view()?.preferences.find(
      (p) => p.category === category && p.channel === channel && !p.strategy_id,
    );
    return found ? found.enabled : true;
  }

  protected async setPref(category: Category, channel: string, event: Event): Promise<void> {
    const box = event.target as HTMLInputElement;
    const enabled = box.checked;
    const saved = await this.save(() =>
      this.api.updatePreferences({ preferences: [{ category, channel, enabled }] }),
    );
    if (!saved) box.checked = !enabled;
  }

  protected async setEventAlert(topic: string, event: Event): Promise<void> {
    const box = event.target as HTMLInputElement;
    const enabled = box.checked;
    const saved = await this.save(() =>
      this.api.updatePreferences({ event_alerts: [{ topic, enabled }] }),
    );
    if (!saved) box.checked = !enabled;
  }

  protected async setImportance(event: Event): Promise<void> {
    const select = event.target as HTMLSelectElement;
    const before = this.economic()?.min_importance ?? 'high';
    const value = select.value as typeof before;
    const saved = await this.save(() =>
      this.api.updatePreferences({ economic_alerts: { min_importance: value } }),
    );
    if (!saved) select.value = before;
  }

  protected async setCountry(code: string, event: Event): Promise<void> {
    const box = event.target as HTMLInputElement;
    const current = this.economic()?.countries ?? [];
    const next = box.checked ? [...current, code] : current.filter((c) => c !== code);
    if (next.length === 0) {
      box.checked = true;
      this.countryError.set('Keep at least one country, or turn off Economic releases coming up.');
      return;
    }
    this.countryError.set(null);
    const saved = await this.save(() =>
      this.api.updatePreferences({ economic_alerts: { countries: next } }),
    );
    if (!saved) box.checked = !box.checked;
  }

  protected async followPortfolios(): Promise<void> {
    this.countryError.set(null);
    const saved = await this.save(() =>
      this.api.updatePreferences({ economic_alerts: { default_countries: true } }),
    );
    if (saved) this.toasts.success('Economic alerts follow your portfolio currencies again.');
  }

  protected async saveQuiet(): Promise<void> {
    const start = this.quietStart();
    const end = this.quietEnd();
    if (!start || !end) {
      this.quietError.set('Pick both times, or turn quiet hours off.');
      return;
    }
    this.quietError.set(null);
    if (await this.save(() => this.api.setQuietHours({ start, end }))) {
      this.toasts.success(`Quiet from ${start} until ${end}.`);
    }
  }

  protected async clearQuiet(): Promise<void> {
    this.quietError.set(null);
    if (await this.save(() => this.api.setQuietHours({ start: null, end: null }))) {
      this.toasts.success('Quiet hours are off.');
    }
  }

  protected async saveWebhook(): Promise<void> {
    const url = this.webhookUrl().trim();
    if (!/^https:\/\/[^/\s]+/i.test(url)) {
      this.webhookError.set('Enter an address that starts with https://');
      return;
    }
    this.webhookError.set(null);
    if (await this.save(() => this.api.setWebhook(url))) {
      this.webhookUrl.set('');
      this.toasts.success('Saved the webhook.');
    }
  }

  protected async removeWebhook(): Promise<void> {
    const ok = await this.confirm.confirm({
      title: 'Remove your webhook?',
      message: 'Alerts stop going to it. You can add an address again at any time.',
      confirmLabel: 'Remove webhook',
      tone: 'danger',
    });
    if (!ok) return;
    this.webhookError.set(null);
    if (await this.save(() => this.api.setWebhook(null))) {
      this.toasts.success('Removed the webhook.');
    }
  }

  protected readonly testing = signal(false);

  protected async sendTest(): Promise<void> {
    this.testing.set(true);
    try {
      const sent = await this.api.sendTest();
      const names = sent.channels
        .filter((c) => c !== ALWAYS_ON)
        .map((c) => this.channelLabel(c).toLowerCase());
      if (sent.deliveries > 0) {
        const where = names.length ? ` by ${names.join(', ')}` : '';
        const count = sent.deliveries === 1 ? '1 delivery' : `${sent.deliveries} deliveries`;
        this.toasts.success(`Sent a test notification: ${count}${where}. It arrives in seconds.`);
      } else {
        this.toasts.info(
          'Sent a test notification to the app only. Turn on push on this device, or another channel, to get it elsewhere.',
        );
      }
    } catch (err) {
      // The call is silent, so every answer is worded here.
      if (err instanceof ApiError && err.status === 429) {
        this.toasts.info('One test a minute. Wait a moment, then send another.');
      } else {
        this.toasts.error(
          err instanceof ApiError ? err.message : 'Could not send the test notification.',
        );
      }
    } finally {
      this.testing.set(false);
    }
  }

  private async save(call: () => Promise<PreferencesView>): Promise<boolean> {
    this.saving.set(true);
    try {
      this.view.set(await call());
      return true;
    } catch {
      // The error interceptor already showed the API's message.
      return false;
    } finally {
      this.saving.set(false);
    }
  }
}
