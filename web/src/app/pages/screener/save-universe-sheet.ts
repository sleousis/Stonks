import { ChangeDetectionStrategy, Component, computed, inject, signal } from '@angular/core';

import type { ScreenSpec, ScreenUniverseView } from '../../api/models';
import { ScreenerService } from '../../api/screener.service';
import { isoDay } from '../../core/format/format';
import { errorMessage } from '../../core/http/api-error';
import { type SegmentOption, Segmented } from '../../shared/ui/segmented';
import { Sheet } from '../../shared/ui/sheet';
import { addDays } from '../calendar/calendar-view';
import { UNIVERSE_ID, universeSlug } from './screen-form';

type Mode = 'rule' | 'snapshot';
type Rebalance = 'weekly' | 'monthly' | 'quarterly';

const MODES: SegmentOption<Mode>[] = [
  { value: 'rule', label: 'Re-run the screen' },
  { value: 'snapshot', label: "Today's matches" },
];
const REBALANCES: SegmentOption<Rebalance>[] = [
  { value: 'weekly', label: 'Weekly' },
  { value: 'monthly', label: 'Monthly' },
  { value: 'quarterly', label: 'Quarterly' },
];

/** The survivorship warning a snapshot always carries. */
export const SNAPSHOT_WARNING =
  "Today's matches are a fixed list. Companies that failed, merged or were delisted since " +
  'are missing from it, so a backtest on it looks better than trading it would have been.';

interface Open {
  spec: ScreenSpec;
  screenId: string | null;
  name: string;
  resolve: (result: ScreenUniverseView | null) => void;
}

/**
 * Save a screen as a universe for the lab. **Re-run the screen** (rule mode)
 * stores a universe that runs the screen on each rebalance date from the
 * start, so the lab sees who passed on each day, dead names too.
 * **Today's matches** (snapshot mode) stores the current rows as a fixed
 * list, with the survivorship warning. Resolves to the saved universe, or
 * null when cancelled.
 *
 *   const saved = await this.sheet().open(spec, screenId, name);
 */
@Component({
  selector: 'app-save-universe-sheet',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [Sheet, Segmented],
  template: `
    <app-sheet
      [open]="!!current()"
      [wide]="true"
      labelledBy="su-title"
      describedBy="su-message"
      (dismiss)="close(null)"
    >
      @if (current(); as c) {
        <form class="sheet-form" novalidate (submit)="$event.preventDefault(); submit()">
          <h2 id="su-title">Save as a universe</h2>
          <p id="su-message" class="sheet-message">
            The lab can then test strategies on it. Its members appear once the refresh finishes.
          </p>

          <div class="field">
            <label for="su-name">Name</label>
            <input
              id="su-name"
              class="input"
              maxlength="120"
              [value]="name()"
              (input)="setName($any($event.target).value)"
            />
          </div>
          <div class="field">
            <label for="su-id">Short name</label>
            <input
              id="su-id"
              class="input num"
              maxlength="64"
              autocapitalize="none"
              spellcheck="false"
              aria-describedby="su-id-hint"
              [attr.aria-invalid]="tried() && !!idError()"
              [value]="universeId()"
              (input)="setId($any($event.target).value)"
            />
            <span id="su-id-hint" class="hint" [class.error]="tried() && !!idError()">
              {{ tried() && idError() ? idError() : 'Lower case letters, digits and dashes.' }}
            </span>
          </div>

          <div class="field">
            <span class="label" id="su-mode-label">Members</span>
            <app-segmented
              label="Members"
              [options]="modes"
              [value]="mode()"
              (valueChange)="mode.set($any($event))"
            />
          </div>

          @if (mode() === 'rule') {
            <p class="hint">
              The screen runs again on each rebalance date, on the data known that day. Companies
              that later failed stay in, so backtests stay honest.
            </p>
            <div class="row">
              <div class="field">
                <label for="su-start">From</label>
                <input
                  id="su-start"
                  class="input"
                  type="date"
                  aria-describedby="su-start-hint"
                  [value]="start()"
                  (change)="start.set($any($event.target).value)"
                />
                <span id="su-start-hint" class="hint">A year ago unless you pick.</span>
              </div>
              <div class="field">
                <span class="label">Rebalance</span>
                <app-segmented
                  label="Rebalance"
                  [options]="rebalances"
                  [value]="rebalance()"
                  (valueChange)="rebalance.set($any($event))"
                />
              </div>
            </div>
          } @else {
            <p class="warning" role="note">
              <strong>Survivorship bias.</strong> {{ snapshotWarning }}
            </p>
          }

          @if (failure(); as f) {
            <p class="failure" role="alert">{{ f }}</p>
          }
          <div class="sheet-actions">
            <button type="button" class="btn" (click)="close(null)">Cancel</button>
            <button type="submit" class="btn btn-primary" [disabled]="busy()">Save universe</button>
          </div>
        </form>
      }
    </app-sheet>
  `,
  styles: `
    .label {
      font-size: var(--text-sm);
      font-weight: var(--weight-medium);
    }
    .row {
      display: grid;
      gap: var(--space-3);
    }
    .hint {
      font-size: var(--text-xs);
      color: var(--color-ink-3);
    }
    .hint.error {
      color: var(--color-loss);
    }
    .warning {
      padding: var(--space-2) var(--space-3);
      border-left: 3px solid var(--color-warn);
      border-radius: var(--radius-sm);
      background: var(--color-warn-soft);
      font-size: var(--text-sm);
    }
    .failure {
      padding: var(--space-3);
      border-left: 3px solid var(--color-loss);
      background: var(--color-loss-soft);
      overflow-wrap: anywhere;
    }
  `,
})
export class SaveUniverseSheet {
  private readonly api = inject(ScreenerService);

  protected readonly modes = MODES;
  protected readonly rebalances = REBALANCES;
  protected readonly snapshotWarning = SNAPSHOT_WARNING;

  protected readonly current = signal<Open | null>(null);
  protected readonly name = signal('');
  protected readonly universeId = signal('');
  /** The short name follows the name until the trader edits it. */
  private readonly idEdited = signal(false);
  protected readonly mode = signal<Mode>('rule');
  protected readonly start = signal('');
  protected readonly rebalance = signal<Rebalance>('monthly');
  protected readonly busy = signal(false);
  protected readonly tried = signal(false);
  protected readonly failure = signal<string | null>(null);

  protected readonly idError = computed(() =>
    UNIVERSE_ID.test(this.universeId())
      ? null
      : 'Use 1 to 64 lower case letters, digits, dots, dashes or underscores, starting with a letter or digit.',
  );

  open(
    spec: ScreenSpec,
    screenId: string | null,
    name: string,
  ): Promise<ScreenUniverseView | null> {
    this.current()?.resolve(null);
    const title = name.trim() || 'My screen';
    this.name.set(title);
    this.universeId.set(universeSlug(title));
    this.idEdited.set(false);
    this.mode.set('rule');
    this.start.set(addDays(isoDay(), -365));
    this.rebalance.set('monthly');
    this.tried.set(false);
    this.failure.set(null);
    return new Promise((resolve) => this.current.set({ spec, screenId, name: title, resolve }));
  }

  protected setName(value: string): void {
    this.name.set(value);
    if (!this.idEdited()) this.universeId.set(universeSlug(value));
  }

  protected setId(value: string): void {
    this.idEdited.set(true);
    this.universeId.set(value.trim().toLowerCase());
  }

  protected async submit(): Promise<void> {
    const c = this.current();
    if (!c) return;
    this.tried.set(true);
    if (this.idError()) return;
    this.busy.set(true);
    this.failure.set(null);
    const rule = this.mode() === 'rule';
    try {
      const saved = await this.api.saveAsUniverse({
        universe_id: this.universeId(),
        name: this.name().trim() || null,
        mode: this.mode(),
        // A saved screen with no changes goes by id, so the universe follows it.
        ...(c.screenId ? { screen_id: c.screenId } : { spec: c.spec }),
        ...(rule ? { start: this.start() || null, rebalance: this.rebalance() } : {}),
      });
      this.close(saved);
    } catch (err) {
      this.failure.set(errorMessage(err));
    } finally {
      this.busy.set(false);
    }
  }

  protected close(result: ScreenUniverseView | null): void {
    const c = this.current();
    this.current.set(null);
    c?.resolve(result);
  }
}
