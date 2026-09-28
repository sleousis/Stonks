import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  resource,
  signal,
} from '@angular/core';

import type { SystemSettingView } from '../../api/generated/types.gen';
import { SystemService } from '../../api/system.service';
import { StepUpService } from '../../core/auth/step-up.service';
import { ApiError } from '../../core/http/api-error';
import { formatAgo, formatDateTime } from '../../core/format/format';
import { ToastService } from '../../core/notify/toast.service';
import { ErrorState, LoadingState } from '../../shared/ui/states';

/** A field's value as the input holds it: text for numbers and lists, a flag for switches. */
type Raw = string | boolean;

/** How a setting is edited, read from its value, default and choices. */
export type SettingKind = 'bool' | 'number' | 'choice' | 'list' | 'json' | 'text';

/** The shortest reason the audit log accepts. */
export const MIN_REASON = 5;

/** The groups in the order the form shows them, with their headings. */
export const GROUPS: readonly { id: SystemSettingView['group']; label: string; help: string }[] = [
  { id: 'risk', label: 'Risk limits', help: 'The system policy every portfolio starts from.' },
  { id: 'trading', label: 'Trading run', help: 'What the daily trading run trades and with what.' },
  { id: 'notifications', label: 'System alerts', help: 'Where operator alerts go.' },
  { id: 'schedule', label: 'Schedule', help: 'Which jobs run and when.' },
];

/** The option a choice setting shows when its value is not one of the choices. */
export const KEEP_CURRENT = '__current__';

const isNum = (v: unknown) => typeof v === 'number';
const isStrList = (v: unknown) => Array.isArray(v) && v.every((x) => typeof x === 'string');

/** How to edit a setting. The API sends plain values, so the kind is read from them. */
export function kindOf(item: SystemSettingView): SettingKind {
  if (item.choices?.length) return 'choice';
  const { value, default: base } = item;
  if (typeof value === 'boolean' || typeof base === 'boolean') return 'bool';
  if ((isNum(value) || value === null) && (isNum(base) || base === null)) {
    // Both empty: an optional limit ("Empty means no limit") is still a number.
    if (isNum(value) || isNum(base) || /\bEmpty\b/.test(item.help)) return 'number';
  }
  if (isStrList(value) || isStrList(base)) {
    const nested = [value, base].some((v) => Array.isArray(v) && v.some(Array.isArray));
    return nested ? 'json' : 'list';
  }
  if ((value !== null && typeof value === 'object') || (base !== null && typeof base === 'object'))
    return 'json';
  return 'text';
}

/** A number setting that may be empty (no limit). */
export function optional(item: SystemSettingView): boolean {
  return item.value === null || item.default === null || /\bEmpty\b/.test(item.help);
}

/** What the input shows for a stored value. */
export function toRaw(item: SystemSettingView, value: unknown): Raw {
  const kind = kindOf(item);
  if (kind === 'bool') return value === true;
  if (value == null) return '';
  if (kind === 'choice') {
    return typeof value === 'string' && (item.choices ?? []).includes(value) ? value : KEEP_CURRENT;
  }
  if (kind === 'list' && Array.isArray(value)) return value.join(', ');
  if (kind === 'json') return JSON.stringify(value);
  return String(value);
}

/** The value to send for what the input holds, or an error in words. */
export function parseRaw(item: SystemSettingView, raw: Raw): { value?: unknown; error?: string } {
  switch (kindOf(item)) {
    case 'bool':
      return { value: raw === true };
    case 'number': {
      const text = String(raw).trim();
      if (text === '') return optional(item) ? { value: null } : { error: 'Enter a number.' };
      const n = Number(text);
      if (!Number.isFinite(n)) return { error: 'Enter a number.' };
      return { value: n };
    }
    case 'choice':
      if (raw === KEEP_CURRENT) return { value: item.value };
      return (item.choices ?? []).includes(String(raw))
        ? { value: String(raw) }
        : { error: 'Pick one of the options.' };
    case 'list':
      return {
        value: String(raw)
          .split(/[\s,]+/)
          .map((s) => s.trim())
          .filter(Boolean),
      };
    case 'json': {
      const text = String(raw).trim();
      if (text === '') return { value: null };
      try {
        return { value: JSON.parse(text) };
      } catch {
        return { error: 'Write it like the default, for example [[0.1, 0.5]].' };
      }
    }
    default:
      return { value: String(raw) };
  }
}

/** A value in words, for the default and the current value of a choice. */
export function valueText(value: unknown): string {
  if (value == null || value === '') return 'None';
  if (value === true) return 'On';
  if (value === false) return 'Off';
  if (Array.isArray(value))
    return value.length ? value.map((v) => (Array.isArray(v) ? `[${v}]` : v)).join(', ') : 'None';
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}

/** The message a 422 gives for one key: the server writes "<key>: <message>". */
export function keyError(err: unknown, key: string): string | null {
  if (!(err instanceof ApiError) || err.status !== 422) return null;
  const detail = String(err.problem['detail'] ?? err.message);
  const prefix = `${key}: `;
  return detail.startsWith(prefix) ? detail.slice(prefix.length) : detail;
}

const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);

/**
 * Admin System settings (`/api/settings/system`): the safe, non-secret
 * settings an admin may change here, grouped as the server groups them,
 * stored as overrides on the TOML config with an audit row. Each changed
 * setting is one PUT with the shared reason, after a fresh second factor.
 * Keys, tokens and passwords never show.
 */
@Component({
  selector: 'app-system-settings',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ErrorState, LoadingState],
  template: `
    <section class="panel" aria-labelledby="ops-settings-title">
      <div class="panel-head">
        <h3 id="ops-settings-title">System settings</h3>
        @if (lastSaved(); as s) {
          <span class="muted saved">
            Saved {{ ago(s.updated_at!) }}{{ s.updated_by ? ' by ' + s.updated_by : '' }}
          </span>
        }
      </div>
      @if (state.error(); as err) {
        <app-error-state
          title="Could not load the system settings"
          [error]="err"
          (retry)="state.reload()"
        />
      } @else if (state.isLoading() && !state.hasValue()) {
        <app-loading-state label="Loading system settings" [rows]="4" />
      } @else {
        <form class="panel-body" novalidate (submit)="$event.preventDefault(); save()">
          <p class="intro">
            Change these here. The server checks each value and keeps it in the audit log with your
            reason. Your authenticator code is asked for before a save. Keys, tokens and passwords
            stay on the server and never show here.
          </p>
          @if (items().length === 0) {
            <p class="muted">The server offers no settings to change here.</p>
          }
          @for (g of groups(); track g.id) {
            <fieldset class="group">
              <legend>{{ g.label }}</legend>
              <p class="group-help">{{ g.help }}</p>
              @for (f of g.items; track f.key) {
                @let id = 'set-' + f.key;
                @let err = shownError(f.key);
                @let kind = kindOf(f);
                <div class="field">
                  @if (kind === 'bool') {
                    <label class="check">
                      <input
                        type="checkbox"
                        [id]="id"
                        [checked]="raw(f) === true"
                        [attr.aria-describedby]="id + '-hint'"
                        (change)="edit(f, $any($event.target).checked)"
                      />
                      {{ f.label }}
                    </label>
                  } @else {
                    <label [for]="id">{{ f.label }}</label>
                    <div class="control">
                      @if (kind === 'choice') {
                        <select
                          class="input"
                          [id]="id"
                          [value]="raw(f)"
                          [attr.aria-invalid]="err ? 'true' : null"
                          [attr.aria-describedby]="id + '-hint' + (err ? ' ' + id + '-error' : '')"
                          (change)="edit(f, $any($event.target).value)"
                        >
                          @if (toRaw(f, f.value) === keep) {
                            <option [value]="keep" [selected]="raw(f) === keep">
                              {{ valueText(f.value) }}
                            </option>
                          }
                          @for (c of f.choices ?? []; track c) {
                            <option [value]="c" [selected]="c === raw(f)">{{ c }}</option>
                          }
                        </select>
                      } @else if (kind === 'list' || kind === 'json') {
                        <textarea
                          class="input"
                          rows="2"
                          [id]="id"
                          [value]="raw(f)"
                          spellcheck="false"
                          [attr.aria-invalid]="err ? 'true' : null"
                          [attr.aria-describedby]="id + '-hint' + (err ? ' ' + id + '-error' : '')"
                          (input)="edit(f, $any($event.target).value)"
                        ></textarea>
                      } @else {
                        <input
                          class="input"
                          [id]="id"
                          type="text"
                          [attr.inputmode]="kind === 'number' ? 'decimal' : null"
                          [value]="raw(f)"
                          [attr.aria-invalid]="err ? 'true' : null"
                          [attr.aria-describedby]="id + '-hint' + (err ? ' ' + id + '-error' : '')"
                          (input)="edit(f, $any($event.target).value)"
                        />
                      }
                    </div>
                  }
                  <span class="hint" [id]="id + '-hint'">{{ hint(f) }}</span>
                  @if (f.overridden && f.updated_at) {
                    <span class="changed">
                      Changed {{ ago(f.updated_at) }}{{ f.updated_by ? ' by ' + f.updated_by : ''
                      }}{{ f.reason ? ': ' + f.reason : '' }}.
                      <button
                        type="button"
                        class="btn btn-ghost"
                        [disabled]="saving()"
                        (click)="reset(f)"
                      >
                        Use the default
                      </button>
                    </span>
                  }
                  @if (f.problem) {
                    <span class="error">The saved value is not used: {{ f.problem }}</span>
                  }
                  @if (err) {
                    <span class="error" [id]="id + '-error'">{{ err }}</span>
                  }
                </div>
              }
            </fieldset>
          }
          @if (items().length) {
            <div class="field reason">
              <label for="ops-settings-reason">Reason for the change</label>
              <input
                id="ops-settings-reason"
                class="input"
                [value]="reason()"
                maxlength="500"
                [attr.aria-invalid]="reasonError() ? 'true' : null"
                aria-describedby="ops-settings-reason-hint"
                (input)="reason.set($any($event.target).value)"
              />
              <span id="ops-settings-reason-hint" class="hint">
                Kept in the audit log with what changed.
              </span>
              @if (reasonError(); as r) {
                <span class="error">{{ r }}</span>
              }
            </div>
            @if (formError(); as s) {
              <p class="form-error" role="alert">{{ s }}</p>
            }
            <div class="actions">
              <button
                type="submit"
                class="btn btn-primary"
                [disabled]="saving() || changedCount() === 0"
                [attr.aria-busy]="saving()"
              >
                {{ saving() ? 'Saving…' : saveLabel() }}
              </button>
              <button
                type="button"
                class="btn btn-ghost"
                [disabled]="saving() || changedCount() === 0"
                (click)="undo()"
              >
                Undo changes
              </button>
            </div>
          }
        </form>
      }
    </section>
  `,
  styles: `
    @use 'breakpoints' as bp;
    .panel-body {
      display: grid;
      gap: var(--space-4);
    }
    .intro,
    .group-help {
      margin: 0;
      font-size: var(--text-sm);
      color: var(--color-ink-2);
      max-width: 65ch;
    }
    .saved,
    .changed {
      font-size: var(--text-sm);
      color: var(--color-ink-2);
    }
    .changed {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: var(--space-2);
    }
    .group {
      display: grid;
      gap: var(--space-3);
      margin: 0;
      padding: 0;
      border: 0;
      min-width: 0;
      legend {
        padding: 0;
        margin-bottom: var(--space-2);
        font-weight: var(--weight-semibold);
      }
    }
    .control {
      display: flex;
      align-items: center;
      gap: var(--space-2);
      .input {
        max-width: 22rem;
      }
      textarea.input {
        max-width: none;
        padding-block: var(--space-2);
      }
    }
    .reason .input {
      max-width: 36rem;
    }
    .form-error {
      margin: 0;
      padding: var(--space-2) var(--space-3);
      border-left: 3px solid var(--color-loss);
      background: var(--color-loss-soft);
      font-size: var(--text-sm);
    }
    .actions {
      display: flex;
      flex-wrap: wrap;
      gap: var(--space-2);
      @include bp.phone {
        .btn {
          flex: 1;
        }
      }
    }
  `,
})
export class SystemSettings {
  private readonly api = inject(SystemService);
  private readonly stepUp = inject(StepUpService);
  private readonly toasts = inject(ToastService);

  protected readonly keep = KEEP_CURRENT;
  protected readonly toRaw = toRaw;
  protected readonly valueText = valueText;
  protected readonly kindOf = kindOf;

  protected readonly state = resource({ loader: () => this.api.settings() });
  /** Items the server sent back after a save, by key: newer than the list. */
  private readonly saved = signal<Record<string, SystemSettingView>>({});
  protected readonly items = computed<SystemSettingView[]>(() => {
    const list = this.state.hasValue() ? this.state.value().items : [];
    const saved = this.saved();
    return list.map((i) => saved[i.key] ?? i);
  });
  protected readonly groups = computed(() =>
    GROUPS.map((g) => ({ ...g, items: this.items().filter((i) => i.group === g.id) })).filter(
      (g) => g.items.length,
    ),
  );
  /** The most recent change of any setting. */
  protected readonly lastSaved = computed(() =>
    this.items()
      .filter((i) => i.overridden && i.updated_at)
      .sort((a, b) => (b.updated_at ?? '').localeCompare(a.updated_at ?? ''))
      .at(0),
  );

  private readonly edits = signal<Record<string, Raw>>({});
  protected readonly reason = signal('');
  protected readonly saving = signal(false);
  protected readonly submitted = signal(false);
  private readonly serverErrors = signal<Record<string, string>>({});
  protected readonly formError = signal<string | null>(null);

  protected raw(f: SystemSettingView): Raw {
    const edits = this.edits();
    return f.key in edits ? edits[f.key] : toRaw(f, f.value);
  }

  /** The changed settings with their parsed values, and errors found here. */
  private readonly changes = computed(() => {
    const values: Record<string, unknown> = {};
    const errors: Record<string, string> = {};
    const edits = this.edits();
    for (const f of this.items()) {
      if (!(f.key in edits)) continue;
      const parsed = parseRaw(f, edits[f.key]);
      if (parsed.error) errors[f.key] = parsed.error;
      else if (!same(parsed.value, f.value)) values[f.key] = parsed.value;
    }
    return { values, errors };
  });
  protected readonly changedCount = computed(
    () => Object.keys(this.changes().values).length + Object.keys(this.changes().errors).length,
  );
  protected readonly saveLabel = computed(() => {
    const n = this.changedCount();
    return n === 0 ? 'Save changes' : n === 1 ? 'Save 1 change' : `Save ${n} changes`;
  });
  protected readonly reasonError = computed(() => {
    if (!this.submitted()) return null;
    return this.reason().trim().length < MIN_REASON
      ? `Say why in a few words (at least ${MIN_REASON} characters).`
      : null;
  });

  protected shownError(key: string): string | null {
    return this.changes().errors[key] ?? this.serverErrors()[key] ?? null;
  }

  protected hint(f: SystemSettingView): string {
    const parts = [f.help];
    parts.push(`Default: ${valueText(f.default)}.`);
    parts.push(
      f.applies === 'restart'
        ? 'Takes effect when the scheduler restarts.'
        : 'Used from the next trading run.',
    );
    return parts.join(' ');
  }

  protected readonly ago = (iso: string) => `${formatAgo(iso)} (${formatDateTime(iso)})`;

  protected edit(f: SystemSettingView, raw: Raw): void {
    this.edits.update((e) => ({ ...e, [f.key]: raw }));
    if (this.serverErrors()[f.key]) {
      this.serverErrors.update((errors) => {
        const rest = { ...errors };
        delete rest[f.key];
        return rest;
      });
    }
  }

  protected undo(): void {
    this.edits.set({});
    this.serverErrors.set({});
    this.formError.set(null);
    this.submitted.set(false);
  }

  private keepSaved(view: SystemSettingView): void {
    this.saved.update((s) => ({ ...s, [view.key]: view }));
    this.edits.update((e) => {
      const rest = { ...e };
      delete rest[view.key];
      return rest;
    });
  }

  /** One PUT per changed setting, with the shared reason, after the second factor. */
  async save(): Promise<void> {
    if (this.saving()) return;
    this.submitted.set(true);
    const { values, errors } = this.changes();
    if (Object.keys(errors).length || this.reasonError()) {
      this.formError.set('Fix the marked fields, then save again.');
      return;
    }
    const keys = Object.keys(values);
    if (!keys.length) return;
    // Busy from here: a second Save during the code check sends nothing twice.
    this.saving.set(true);
    if (!(await this.stepUp.ensure('Change system settings'))) {
      this.saving.set(false);
      return;
    }
    this.formError.set(null);
    const reason = this.reason().trim();
    const failed: Record<string, string> = {};
    let done = 0;
    try {
      for (const key of keys) {
        try {
          this.keepSaved(await this.api.changeSetting(key, { value: values[key], reason }));
          done += 1;
        } catch (err) {
          const message = keyError(err, key);
          if (message === null) throw err;
          failed[key] = message;
        }
      }
    } catch (err) {
      this.formError.set(err instanceof Error ? err.message : String(err));
    } finally {
      this.saving.set(false);
    }
    this.serverErrors.set(failed);
    if (Object.keys(failed).length) {
      this.formError.set('The server did not accept some values. Fix the marked fields.');
    } else if (!this.formError()) {
      this.reason.set('');
      this.submitted.set(false);
    }
    if (done) this.toasts.success(done === 1 ? 'Saved 1 setting.' : `Saved ${done} settings.`);
  }

  /** Drop the override, so the TOML value applies again. Needs the reason too. */
  async reset(f: SystemSettingView): Promise<void> {
    if (this.saving()) return;
    this.submitted.set(true);
    if (this.reasonError()) {
      this.formError.set('Say why in the reason field, then try again.');
      return;
    }
    this.saving.set(true);
    if (!(await this.stepUp.ensure('Change system settings'))) {
      this.saving.set(false);
      return;
    }
    this.formError.set(null);
    try {
      this.keepSaved(await this.api.resetSetting(f.key, { reason: this.reason().trim() }));
      this.submitted.set(false);
      this.toasts.success(`${f.label} uses the default again.`);
    } catch (err) {
      this.formError.set(err instanceof Error ? err.message : String(err));
    } finally {
      this.saving.set(false);
    }
  }
}
