import { ChangeDetectionStrategy, Component, computed, inject } from '@angular/core';

import { formatDateTime, formatMoney, formatPercent } from '../../core/format/format';
import { type FormatPrefs, FormatService, LOCALE_OPTIONS } from '../../core/format/format.service';

const SAMPLE_TIME = '2026-09-26T14:05:00Z';

/**
 * Settings panel: language and region for numbers, time zone for times, and
 * the date style. Shows a live sample so the effect is obvious.
 */
@Component({
  selector: 'app-display-prefs',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <section class="panel" aria-labelledby="display-title">
      <div class="panel-head">
        <h3 id="display-title">Numbers and dates</h3>
      </div>
      <div class="panel-body form-grid">
        <div class="field">
          <label for="pref-locale">Region format</label>
          <select
            id="pref-locale"
            class="input"
            aria-describedby="pref-sample"
            (change)="set('locale', $any($event.target).value)"
          >
            <option value="" [selected]="prefs().locale === null">
              Browser default ({{ fmt.browser.locale }})
            </option>
            @for (o of locales; track o.value) {
              <option [value]="o.value" [selected]="prefs().locale === o.value">
                {{ o.label }}
              </option>
            }
          </select>
        </div>
        <div class="field">
          <label for="pref-zone">Time zone</label>
          <select
            id="pref-zone"
            class="input"
            aria-describedby="pref-sample"
            (change)="set('timeZone', $any($event.target).value)"
          >
            <option value="" [selected]="prefs().timeZone === null">
              Browser default ({{ fmt.browser.timeZone }})
            </option>
            @for (z of zones; track z) {
              <option [value]="z" [selected]="prefs().timeZone === z">{{ z }}</option>
            }
          </select>
        </div>
        <fieldset class="styles">
          <legend>Dates</legend>
          <label class="check">
            <input
              type="radio"
              name="date-style"
              value="iso"
              [checked]="prefs().dateStyle === 'iso'"
              (change)="setDateStyle('iso')"
            />
            2026-09-26
          </label>
          <label class="check">
            <input
              type="radio"
              name="date-style"
              value="locale"
              [checked]="prefs().dateStyle === 'locale'"
              (change)="setDateStyle('locale')"
            />
            Region format
          </label>
        </fieldset>
        <p id="pref-sample" class="sample" aria-live="polite">
          Looks like <span class="num">{{ sample() }}</span>
        </p>
      </div>
    </section>
  `,
  styles: `
    :host {
      display: block;
    }
    .form-grid {
      max-width: none;
    }
    .styles {
      display: flex;
      flex-wrap: wrap;
      gap: var(--space-2) var(--space-5);
      margin: 0;
      padding: 0;
      border: 0;
    }
    legend {
      width: 100%;
      margin-bottom: var(--space-1);
      font-size: var(--text-sm);
      font-weight: var(--weight-medium);
    }
    .sample {
      font-size: var(--text-sm);
      color: var(--color-ink-2);
      overflow-wrap: anywhere;
    }
    .sample .num {
      color: var(--color-ink);
      font-weight: var(--weight-medium);
    }
  `,
})
export class DisplayPrefs {
  protected readonly fmt = inject(FormatService);
  protected readonly prefs = this.fmt.prefs;
  protected readonly locales = LOCALE_OPTIONS;
  protected readonly zones = this.fmt.timeZones();

  protected readonly sample = computed(() => {
    this.fmt.active();
    return `${formatMoney(12345.678)} · ${formatPercent(0.0425, { signed: true })} · ${formatDateTime(SAMPLE_TIME)}`;
  });

  protected set(key: 'locale' | 'timeZone', value: string): void {
    this.fmt.update({ [key]: value || null } as Partial<FormatPrefs>);
  }

  protected setDateStyle(style: FormatPrefs['dateStyle']): void {
    this.fmt.update({ dateStyle: style });
  }
}
