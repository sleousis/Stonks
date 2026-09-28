import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  resource,
  signal,
} from '@angular/core';

import { BriefingsService } from '../../api/briefings.service';
import type { BriefingPrefsView } from '../../api/models';
import { SessionService } from '../../core/auth/session.service';
import { ToastService } from '../../core/notify/toast.service';
import { PermissionNote } from './permission-note';
import { ErrorState, LoadingState } from './states';

type Kind = 'pre_open' | 'post_close';

export const BRIEFING_KINDS: readonly { value: Kind; label: string; help: string }[] = [
  { value: 'pre_open', label: 'Before the open', help: 'Your books, risk and orders for the day.' },
  {
    value: 'post_close',
    label: 'After the close',
    help: 'The day: P&L, fills, signals and halts.',
  },
];

/**
 * Settings: research-only briefings from the assistant. It reads, never
 * changes anything, and every number comes from a tool.
 */
@Component({
  selector: 'app-briefing-prefs',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ErrorState, LoadingState, PermissionNote],
  template: `
    <section class="panel" aria-labelledby="briefings-title">
      <div class="panel-head">
        <h3 id="briefings-title">Briefings</h3>
      </div>
      @if (prefs.error(); as err) {
        <app-error-state title="Could not load briefings" [error]="err" (retry)="prefs.reload()" />
      } @else if (!prefs.hasValue()) {
        <app-loading-state label="Loading briefings" [rows]="2" />
      } @else {
        <div class="panel-body">
          <p class="lead">
            A short note from the assistant through your alert channels. Research only: it reads and
            never changes anything.
          </p>
          @if (!prefs.value().available) {
            <p class="note">Briefings are off on this install.</p>
          }
          @for (k of kinds; track k.value) {
            <label class="switch">
              <input
                type="checkbox"
                [checked]="on(prefs.value(), k.value)"
                [disabled]="!canSave() || busy()"
                (change)="toggle(prefs.value(), k.value, $any($event.target).checked)"
              />
              <span>
                <strong>{{ k.label }}</strong>
                <span class="muted"> {{ k.help }}</span>
              </span>
            </label>
          }
          @if (!canSave()) {
            <app-permission-note permission="notifications.manage" />
          }
        </div>
      }
    </section>
  `,
  styles: `
    :host {
      display: block;
    }
    .panel-body {
      display: grid;
      gap: var(--space-2);
    }
    .lead {
      color: var(--color-ink-2);
    }
    .switch {
      display: flex;
      align-items: flex-start;
      gap: var(--space-2);
      min-height: var(--touch-min);
      cursor: pointer;
    }
    .switch input {
      margin-top: 3px;
    }
    .note {
      padding: var(--space-2) var(--space-3);
      border-left: 3px solid var(--color-info);
      border-radius: var(--radius-sm);
      background: var(--color-info-soft);
      font-size: var(--text-sm);
    }
  `,
})
export class BriefingPrefs {
  private readonly api = inject(BriefingsService);
  private readonly session = inject(SessionService);
  private readonly toasts = inject(ToastService);

  protected readonly kinds = BRIEFING_KINDS;
  protected readonly prefs = resource({ loader: () => this.api.prefs() });
  protected readonly canSave = computed(() => this.session.can('notifications.manage'));
  protected readonly busy = signal(false);

  protected on(p: BriefingPrefsView, kind: Kind): boolean {
    return kind === 'pre_open' ? p.pre_open : p.post_close;
  }

  protected async toggle(p: BriefingPrefsView, kind: Kind, checked: boolean): Promise<void> {
    this.busy.set(true);
    try {
      const next = {
        pre_open: kind === 'pre_open' ? checked : p.pre_open,
        post_close: kind === 'post_close' ? checked : p.post_close,
      };
      this.prefs.set(await this.api.save(next));
      this.toasts.success(checked ? 'Briefing on.' : 'Briefing off.');
    } catch {
      this.toasts.error('Could not save the briefing setting.');
      this.prefs.reload();
    } finally {
      this.busy.set(false);
    }
  }
}
