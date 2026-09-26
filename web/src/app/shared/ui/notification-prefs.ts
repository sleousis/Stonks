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
import { ToastService } from '../../core/notify/toast.service';
import { ErrorState, LoadingState } from './states';

type Category = PreferenceItem['category'];

const CATEGORIES: readonly { value: Category; label: string }[] = [
  { value: 'signal', label: 'Signals' },
  { value: 'order', label: 'Orders and fills' },
  { value: 'risk', label: 'Risk alerts' },
  { value: 'system', label: 'System' },
];

const CHANNEL_LABELS: Record<string, string> = {
  webpush: 'Push',
  email: 'Email',
  webhook: 'Webhook',
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
  imports: [LoadingState, ErrorState],
  template: `
    <section class="panel" aria-labelledby="prefs-title">
      <div class="panel-head">
        <h2 id="prefs-title">Alert settings</h2>
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
          @if (channels().length === 0) {
            <p class="note">This server has no push, email or webhook delivery set up yet.</p>
          } @else {
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
                            [disabled]="saving()"
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
          }

          <fieldset class="quiet">
            <legend>Quiet hours</legend>
            <p class="hint">
              Signals and fills wait for a morning summary. Risk alerts always come through. Times
              are in {{ view()?.timezone }}.
            </p>
            <div class="times">
              <div class="field">
                <label for="quiet-start">From</label>
                <input
                  id="quiet-start"
                  class="input"
                  type="time"
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
                  [value]="quietEnd()"
                  (input)="quietEnd.set($any($event.target).value)"
                />
              </div>
            </div>
            @if (quietError(); as err) {
              <p class="error" role="alert">{{ err }}</p>
            }
            <div class="actions">
              <button type="button" class="btn" [disabled]="saving()" (click)="saveQuiet()">
                Save quiet hours
              </button>
              @if (view()?.quiet_start) {
                <button
                  type="button"
                  class="btn btn-ghost"
                  [disabled]="saving()"
                  (click)="clearQuiet()"
                >
                  Turn off
                </button>
              }
            </div>
          </fieldset>
        </div>
      }
    </section>
  `,
  styles: `
    :host {
      display: block;
    }
    .body {
      display: grid;
      gap: var(--space-4);
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

  protected readonly categories = CATEGORIES;
  protected readonly prefs = resource({ loader: () => this.api.preferences() });
  /** The settings as shown, replaced by each save's response. */
  protected readonly view = linkedSignal<PreferencesView | null>(() =>
    this.prefs.hasValue() ? this.prefs.value() : null,
  );
  protected readonly channels = computed(
    () => this.view()?.channels.filter((c) => c !== ALWAYS_ON) ?? [],
  );
  protected readonly quietStart = linkedSignal(() => this.view()?.quiet_start ?? '');
  protected readonly quietEnd = linkedSignal(() => this.view()?.quiet_end ?? '');
  protected readonly quietError = signal<string | null>(null);
  protected readonly saving = signal(false);

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
