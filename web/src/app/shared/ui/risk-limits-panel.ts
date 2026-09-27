import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  linkedSignal,
  resource,
  signal,
} from '@angular/core';

import type { RiskLimitsView, RiskPolicy } from '../../api/models';
import { RiskService } from '../../api/risk.service';
import { SessionService } from '../../core/auth/session.service';
import { formatMoney, formatNumber, formatPercent } from '../../core/format/format';
import { ToastService } from '../../core/notify/toast.service';
import { PermissionNote } from './permission-note';
import { ErrorState, LoadingState } from './states';

type LimitKey =
  'max_open_positions' | 'max_weight_per_ticker' | 'cash_buffer_fraction' | 'min_order_notional';

interface LimitField {
  key: LimitKey;
  label: string;
  hint: string;
  /** How the value is typed: a count, a percent of value, or money. */
  unit: 'count' | 'percent' | 'money';
}

export const LIMIT_FIELDS: readonly LimitField[] = [
  {
    key: 'max_weight_per_ticker',
    label: 'Largest holding',
    hint: 'Most of your portfolio one ticker may take, in percent.',
    unit: 'percent',
  },
  {
    key: 'max_open_positions',
    label: 'Open positions',
    hint: 'Most tickers held at once.',
    unit: 'count',
  },
  {
    key: 'cash_buffer_fraction',
    label: 'Cash to keep',
    hint: 'Part of your portfolio that stays in cash after buys, in percent.',
    unit: 'percent',
  },
  {
    key: 'min_order_notional',
    label: 'Smallest order',
    hint: 'Buys worth less than this are skipped.',
    unit: 'money',
  },
];

export type LimitDraft = Record<LimitKey, string>;

/** The typed form of your limits: percents as 0 to 100, blank for none. */
export function draftOf(mine: Record<string, unknown>): LimitDraft {
  const out = {} as LimitDraft;
  for (const f of LIMIT_FIELDS) {
    const v = mine[f.key];
    out[f.key] =
      typeof v === 'number' ? String(f.unit === 'percent' ? +(v * 100).toFixed(4) : v) : '';
  }
  return out;
}

/** The body to send, or the first problem to fix. Blank fields are left out. */
export function limitsOf(
  draft: LimitDraft,
): { limits: Record<string, number>; error: null } | { limits: null; error: string } {
  const limits: Record<string, number> = {};
  for (const f of LIMIT_FIELDS) {
    const raw = draft[f.key].trim();
    if (!raw) continue;
    const n = Number(raw);
    if (!Number.isFinite(n) || n < 0) {
      return { limits: null, error: `${f.label} must be a number of 0 or more.` };
    }
    if (f.unit === 'percent') {
      if (n > 100) return { limits: null, error: `${f.label} is a percent, at most 100.` };
      limits[f.key] = n / 100;
    } else if (f.unit === 'count') {
      if (!Number.isInteger(n))
        return { limits: null, error: `${f.label} must be a whole number.` };
      limits[f.key] = n;
    } else {
      limits[f.key] = n;
    }
  }
  return { limits, error: null };
}

/** A policy value for display ("No limit" for none). */
export function shown(policy: RiskPolicy, f: LimitField): string {
  const v = policy[f.key];
  if (v === null || v === undefined) return 'No limit';
  if (f.unit === 'percent') return formatPercent(v as number, { digits: 1 });
  if (f.unit === 'money') return formatMoney(v as number);
  return formatNumber(v as number);
}

/**
 * Your own risk limits: they tighten the system policy on every portfolio
 * you own, and can never loosen it. Each field shows the system limit and
 * what your portfolios follow.
 */
@Component({
  selector: 'app-risk-limits-panel',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [PermissionNote, LoadingState, ErrorState],
  template: `
    <section class="panel" aria-labelledby="limits-title">
      <div class="panel-head">
        <h2 id="limits-title">Your risk limits</h2>
      </div>
      @if (limits.error(); as err) {
        <app-error-state
          title="Could not load your limits"
          [error]="err"
          (retry)="limits.reload()"
        />
      } @else if (!limits.hasValue()) {
        <app-loading-state label="Loading your limits" [rows]="4" />
      } @else {
        @let v = limits.value();
        <form class="panel-body" (submit)="save($event, v)">
          <p class="lead">
            Stricter than the system limits, on every portfolio you own. Leave a field blank for no
            limit of your own.
          </p>
          <div class="fields">
            @for (f of fields; track f.key) {
              <div class="field">
                <label [for]="'limit-' + f.key">{{ f.label }}</label>
                <input
                  class="input num"
                  inputmode="decimal"
                  [id]="'limit-' + f.key"
                  [attr.aria-describedby]="'limit-' + f.key + '-hint'"
                  [disabled]="!canEdit()"
                  [value]="draft()[f.key]"
                  (input)="patch(f.key, $any($event.target).value)"
                />
                <span class="hint" [id]="'limit-' + f.key + '-hint'">
                  {{ f.hint }} System: <span class="num">{{ systemText(v, f) }}</span
                  >. You follow: <span class="num">{{ effectiveText(v, f) }}</span
                  >.
                </span>
              </div>
            }
          </div>
          @if (ignoredText(v); as text) {
            <p class="note" role="status">{{ text }}</p>
          }
          @if (error(); as e) {
            <p class="error" role="alert">{{ e }}</p>
          }
          <div class="actions">
            <button type="submit" class="btn btn-primary" [disabled]="saving() || !canEdit()">
              Save limits
            </button>
            <app-permission-note permission="portfolio.manage" />
          </div>
        </form>
      }
    </section>
  `,
  styles: `
    @use 'breakpoints' as bp;
    .panel-body {
      display: grid;
      gap: var(--space-3);
    }
    .lead {
      color: var(--color-ink-2);
      max-width: 60ch;
    }
    .fields {
      display: grid;
      gap: var(--space-4);
      grid-template-columns: minmax(0, 1fr);

      @include bp.from-tablet {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }
    }
    .note {
      padding: var(--space-2) var(--space-3);
      border-left: 3px solid var(--color-warn);
      border-radius: var(--radius-sm);
      background: var(--color-warn-soft);
      font-size: var(--text-sm);
    }
    .error {
      color: var(--color-loss);
      font-size: var(--text-sm);
    }
    .actions {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: var(--space-2);
    }
  `,
})
export class RiskLimitsPanel {
  private readonly api = inject(RiskService);
  private readonly toasts = inject(ToastService);
  private readonly session = inject(SessionService);

  protected readonly fields = LIMIT_FIELDS;
  protected readonly canEdit = computed(() => this.session.can('portfolio.manage'));
  protected readonly limits = resource({ loader: () => this.api.myLimits() });
  protected readonly draft = linkedSignal<RiskLimitsView | undefined, LimitDraft>({
    source: () => (this.limits.hasValue() ? this.limits.value() : undefined),
    computation: (v) => draftOf(v?.mine ?? {}),
  });
  protected readonly error = signal<string | null>(null);
  protected readonly saving = signal(false);

  protected patch(key: LimitKey, value: string): void {
    this.draft.update((d) => ({ ...d, [key]: value }));
  }

  protected systemText(v: RiskLimitsView, f: LimitField): string {
    return shown(v.system, f);
  }

  protected effectiveText(v: RiskLimitsView, f: LimitField): string {
    return shown(v.effective, f);
  }

  protected ignoredText(v: RiskLimitsView): string | null {
    const names = LIMIT_FIELDS.filter((f) => v.ignored.includes(f.key)).map((f) => f.label);
    return names.length
      ? `Looser than the system limit, so it changes nothing: ${names.join(', ')}.`
      : null;
  }

  protected async save(event: Event, current: RiskLimitsView): Promise<void> {
    event.preventDefault();
    const parsed = limitsOf(this.draft());
    if (parsed.error !== null) {
      this.error.set(parsed.error);
      return;
    }
    // Keep limits this form does not show (set elsewhere) as they are.
    const others = Object.fromEntries(
      Object.entries(current.mine).filter(([k]) => !LIMIT_FIELDS.some((f) => f.key === k)),
    );
    this.saving.set(true);
    this.error.set(null);
    try {
      this.limits.set(await this.api.setMyLimits({ ...others, ...parsed.limits }));
      this.toasts.success('Saved your risk limits.');
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.saving.set(false);
    }
  }
}
