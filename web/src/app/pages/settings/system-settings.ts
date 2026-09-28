import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  resource,
  signal,
} from '@angular/core';

import {
  type AdminSettingsView,
  AdminSettingsService,
  type SettingField,
  settingErrors,
} from '../../api/admin-settings.service';
import { formatAgo, formatDateTime, formatNumber, formatPercent } from '../../core/format/format';
import { ToastService } from '../../core/notify/toast.service';
import { ErrorState, LoadingState } from '../../shared/ui/states';

/** A field's value as the input holds it: text for numbers and lists, a flag for switches. */
type Raw = string | boolean;

/** The shortest reason the audit log accepts. */
export const MIN_REASON = 5;

/** What the input shows for a stored value. Percent fields are fractions on the wire. */
export function toRaw(field: SettingField, value: unknown): Raw {
  if (field.type === 'bool') return value === true;
  if (value == null) return '';
  if (field.type === 'percent' && typeof value === 'number') {
    return String(Number((value * 100).toPrecision(12)));
  }
  if (field.type === 'list' && Array.isArray(value)) return value.join(', ');
  return String(value);
}

/** The value to send for what the input holds, or an error in words. */
export function parseRaw(field: SettingField, raw: Raw): { value?: unknown; error?: string } {
  switch (field.type) {
    case 'bool':
      return { value: raw === true };
    case 'int':
    case 'float':
    case 'percent': {
      const text = String(raw).trim();
      if (text === '') return { error: 'Enter a number.' };
      const n = Number(text);
      if (!Number.isFinite(n)) return { error: 'Enter a number.' };
      if (field.type === 'int' && !Number.isInteger(n)) return { error: 'Enter a whole number.' };
      const unit = field.type === 'percent' ? '%' : field.unit ? ` ${field.unit}` : '';
      if (field.min != null && n < field.min) return { error: `At least ${field.min}${unit}.` };
      if (field.max != null && n > field.max) return { error: `At most ${field.max}${unit}.` };
      return { value: field.type === 'percent' ? n / 100 : n };
    }
    case 'list':
      return {
        value: String(raw)
          .split(/[\s,]+/)
          .map((s) => s.trim())
          .filter(Boolean),
      };
    case 'choice': {
      const ok = (field.choices ?? []).some((c) => c.value === raw);
      return ok ? { value: raw } : { error: 'Pick one of the options.' };
    }
    default:
      return { value: String(raw) };
  }
}

/** A value in words for the "Default" hint. */
export function valueText(field: SettingField, value: unknown): string {
  if (value == null || value === '') return 'None';
  switch (field.type) {
    case 'bool':
      return value ? 'On' : 'Off';
    case 'percent':
      return typeof value === 'number' ? formatPercent(value, { digits: 1 }) : String(value);
    case 'int':
    case 'float':
      return typeof value === 'number'
        ? `${formatNumber(value)}${field.unit ? ` ${field.unit}` : ''}`
        : String(value);
    case 'choice':
      return field.choices?.find((c) => c.value === value)?.label ?? String(value);
    case 'list':
      return Array.isArray(value) ? value.join(', ') || 'None' : String(value);
    default:
      return String(value);
  }
}

const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);

/**
 * Admin System settings: the safe, non-secret switches the server lets an
 * admin change here, grouped, with validation, a reason for the audit log,
 * and each setting's default. Secrets (keys, tokens, passwords) never show.
 *
 * Feature check: the form renders only when `GET /api/admin/settings`
 * answers. A server without the route (404) shows nothing, so the read-only
 * System panels stay as they were.
 */
@Component({
  selector: 'app-system-settings',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ErrorState, LoadingState],
  template: `
    @if (state.error(); as err) {
      <section class="panel" aria-labelledby="ops-settings-title">
        <div class="panel-head"><h3 id="ops-settings-title">Operational settings</h3></div>
        <app-error-state
          title="Could not load the operational settings"
          [error]="err"
          (retry)="state.reload()"
        />
      </section>
    } @else if (state.isLoading() && !state.hasValue()) {
      <section class="panel" aria-labelledby="ops-settings-title">
        <div class="panel-head"><h3 id="ops-settings-title">Operational settings</h3></div>
        <app-loading-state label="Loading operational settings" [rows]="4" />
      </section>
    } @else if (view(); as v) {
      <section class="panel" aria-labelledby="ops-settings-title">
        <div class="panel-head">
          <h3 id="ops-settings-title">Operational settings</h3>
          @if (v.updated_at) {
            <span class="muted saved">
              Saved {{ ago(v.updated_at) }}{{ v.updated_by ? ' by ' + v.updated_by : '' }}
            </span>
          }
        </div>
        <form class="panel-body" novalidate (submit)="$event.preventDefault(); save()">
          <p class="intro">
            Change these here. Each save is checked by the server and kept in the audit log with
            your reason. Keys, tokens and passwords stay on the server and never show here.
          </p>
          @if (v.groups.length === 0) {
            <p class="muted">The server offers no settings to change here yet.</p>
          }
          @for (g of v.groups; track g.id) {
            <fieldset class="group">
              <legend>{{ g.label }}</legend>
              @if (g.description) {
                <p class="group-help">{{ g.description }}</p>
              }
              @for (f of g.fields; track f.key) {
                @let id = 'set-' + f.key;
                @let err = shownError(f.key);
                @if (f.type === 'bool') {
                  <div class="field">
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
                    <span class="hint" [id]="id + '-hint'">{{ hint(f) }}</span>
                  </div>
                } @else {
                  <div class="field">
                    <label [for]="id">{{ f.label }}</label>
                    <div class="control">
                      @if (f.type === 'choice') {
                        <select
                          class="input"
                          [id]="id"
                          [value]="raw(f)"
                          [attr.aria-invalid]="err ? 'true' : null"
                          [attr.aria-describedby]="id + '-hint' + (err ? ' ' + id + '-error' : '')"
                          (change)="edit(f, $any($event.target).value)"
                        >
                          @for (c of f.choices ?? []; track c.value) {
                            <option [value]="c.value" [selected]="c.value === raw(f)">
                              {{ c.label }}
                            </option>
                          }
                        </select>
                      } @else if (f.type === 'list') {
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
                          [attr.inputmode]="f.type === 'string' ? null : 'decimal'"
                          [value]="raw(f)"
                          [attr.aria-invalid]="err ? 'true' : null"
                          [attr.aria-describedby]="id + '-hint' + (err ? ' ' + id + '-error' : '')"
                          (input)="edit(f, $any($event.target).value)"
                        />
                      }
                      @if (f.type === 'percent') {
                        <span class="unit" aria-hidden="true">%</span>
                      } @else if (f.unit && f.type !== 'string') {
                        <span class="unit" aria-hidden="true">{{ f.unit }}</span>
                      }
                    </div>
                    <span class="hint" [id]="id + '-hint'">{{ hint(f) }}</span>
                    @if (err) {
                      <span class="error" [id]="id + '-error'">{{ err }}</span>
                    }
                  </div>
                }
              }
            </fieldset>
          }
          @if (v.groups.length) {
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
            @if (serverError(); as s) {
              <p class="form-error" role="alert">{{ s }}</p>
            }
            @if (restartNeeded()) {
              <p class="muted">Some of these changes take effect after the server restarts.</p>
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
      </section>
    }
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
    .saved {
      font-size: var(--text-sm);
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
    .unit {
      font-size: var(--text-sm);
      color: var(--color-ink-2);
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
  private readonly api = inject(AdminSettingsService);
  private readonly toasts = inject(ToastService);

  protected readonly state = resource({ loader: () => this.api.load() });
  /** Set after a save, so the form shows what the server kept. */
  private readonly saved = signal<AdminSettingsView | null>(null);
  protected readonly view = computed<AdminSettingsView | null>(() => {
    const saved = this.saved();
    if (saved) return saved;
    const s = this.state.hasValue() ? this.state.value() : null;
    return s?.available ? s.view : null;
  });
  /** False only once the server said it has no editable settings. */
  readonly available = computed(() =>
    this.state.hasValue() ? this.state.value().available : true,
  );

  private readonly fields = computed(() => this.view()?.groups.flatMap((g) => g.fields) ?? []);
  private readonly edits = signal<Record<string, Raw>>({});
  protected readonly reason = signal('');
  protected readonly saving = signal(false);
  protected readonly submitted = signal(false);
  private readonly serverErrors = signal<Record<string, string>>({});
  protected readonly serverError = signal<string | null>(null);

  protected raw(f: SettingField): Raw {
    const edits = this.edits();
    return f.key in edits ? edits[f.key] : toRaw(f, f.value);
  }

  /** The changed settings with their parsed values, and client-side errors. */
  private readonly changes = computed(() => {
    const values: Record<string, unknown> = {};
    const errors: Record<string, string> = {};
    const edits = this.edits();
    for (const f of this.fields()) {
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
  protected readonly restartNeeded = computed(() =>
    this.fields().some((f) => f.restart && f.key in this.changes().values),
  );
  protected readonly reasonError = computed(() => {
    if (!this.submitted()) return null;
    return this.reason().trim().length < MIN_REASON
      ? `Say why in a few words (at least ${MIN_REASON} characters).`
      : null;
  });

  protected shownError(key: string): string | null {
    return this.changes().errors[key] ?? this.serverErrors()[key] ?? null;
  }

  protected hint(f: SettingField): string {
    const parts: string[] = [];
    if (f.help) parts.push(f.help);
    if (f.default !== undefined) parts.push(`Default: ${valueText(f, f.default)}.`);
    if (f.restart) parts.push('Takes effect after a restart.');
    return parts.join(' ');
  }

  protected readonly ago = (iso: string) => `${formatAgo(iso)} (${formatDateTime(iso)})`;

  protected edit(f: SettingField, raw: Raw): void {
    this.edits.update((e) => ({ ...e, [f.key]: raw }));
    if (this.serverErrors()[f.key]) {
      this.serverErrors.update(({ [f.key]: _gone, ...rest }) => rest);
    }
  }

  protected undo(): void {
    this.edits.set({});
    this.serverErrors.set({});
    this.serverError.set(null);
    this.submitted.set(false);
  }

  async save(): Promise<void> {
    this.submitted.set(true);
    const { values, errors } = this.changes();
    if (Object.keys(errors).length || this.reasonError()) {
      this.serverError.set('Fix the marked fields, then save again.');
      return;
    }
    if (!Object.keys(values).length) return;
    this.saving.set(true);
    this.serverError.set(null);
    try {
      const view = await this.api.save({ values, reason: this.reason().trim() });
      this.saved.set(view);
      const n = Object.keys(values).length;
      this.edits.set({});
      this.serverErrors.set({});
      this.reason.set('');
      this.submitted.set(false);
      this.toasts.success(n === 1 ? 'Saved 1 setting.' : `Saved ${n} settings.`);
    } catch (err) {
      const { byKey, other } = settingErrors(
        err,
        this.fields().map((f) => f.key),
      );
      this.serverErrors.set(byKey);
      this.serverError.set(
        other ?? 'The server did not accept some values. Fix the marked fields and save again.',
      );
    } finally {
      this.saving.set(false);
    }
  }
}
