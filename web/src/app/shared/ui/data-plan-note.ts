import {
  ChangeDetectionStrategy,
  Component,
  Injectable,
  computed,
  inject,
  input,
  resource,
} from '@angular/core';
import { RouterLink } from '@angular/router';

import { MarketService } from '../../api/market.service';
import type { DataCoverage } from '../../api/models';
import { SessionService } from '../../core/auth/session.service';

/** The kinds of data that come with a paid plan (`GET /api/market/data-coverage`). */
export type PaidDataKind = keyof DataCoverage;

interface PlanCopy {
  /** What is missing, as a sentence. */
  what: string;
  /** What an admin does about it. */
  admin: string;
  /** Where the admin does it. */
  link: { path: string; label: string };
}

export const PLAN_COPY: Readonly<Record<PaidDataKind, PlanCopy>> = {
  calendars: {
    what: 'Earnings, dividend and economic calendars come with a paid data plan, and none are stored yet.',
    admin: 'Use a data plan that includes calendars, then run the calendar update.',
    link: { path: '/ops/schedule', label: 'Open the schedule' },
  },
  news: {
    what: 'News and its mood scores come with a paid data plan, and none are stored yet.',
    admin: 'Use a data plan that includes news, then update the data.',
    link: { path: '/data', label: 'Open Data' },
  },
  fundamentals: {
    what: 'Company numbers such as earnings, sales and debt come with a paid data plan, and none are stored yet. Filters and factors on them match nothing until then.',
    admin: 'Use a data plan that includes fundamentals, then update the data.',
    link: { path: '/data', label: 'Open Data' },
  },
  options: {
    what: 'Option chains need an options data plan, and none are stored yet.',
    admin: 'Load option chains from a plan that includes them.',
    link: { path: '/data', label: 'Open Data' },
  },
};

/**
 * Which paid data kinds the lake holds, read once per session and shared
 * by every page that shows a data-plan note. A failed read hides the notes
 * (the page's own empty text still shows).
 */
@Injectable({ providedIn: 'root' })
export class DataCoverageService {
  private readonly market = inject(MarketService);
  readonly coverage = resource({ loader: () => this.market.dataCoverage() });

  /** `true` when stored, `false` when missing, `null` while unknown. */
  has(kind: PaidDataKind): boolean | null {
    return this.coverage.hasValue() ? this.coverage.value()[kind] : null;
  }
}

/**
 * Says plainly that a page is empty because the data plan lacks a kind of
 * data, and what the admin can do, instead of an empty page that looks
 * broken (audit F49). Renders nothing while the data is stored or unknown.
 *
 *   <app-data-plan-note kind="calendars" />
 */
@Component({
  selector: 'app-data-plan-note',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink],
  template: `
    @if (missing()) {
      @let c = copy();
      <div class="plan-note" role="note" [attr.aria-label]="'Needs a data plan'">
        <p class="title">Needs a data plan</p>
        <p>{{ c.what }}</p>
        @if (isAdmin()) {
          <p>
            {{ c.admin }}
            <a class="link" [routerLink]="c.link.path">{{ c.link.label }}</a>
          </p>
        } @else {
          <p>Your admin can add a plan that includes it.</p>
        }
      </div>
    }
  `,
  styles: `
    @use 'breakpoints' as bp;
    :host {
      display: block;
    }
    :host(:empty) {
      display: none;
    }
    .plan-note {
      display: grid;
      gap: var(--space-1);
      padding: var(--space-3) var(--space-4);
      border: 1px solid var(--color-border);
      border-left: 3px solid var(--color-warn, var(--color-accent));
      border-radius: var(--radius-sm);
      background: var(--color-surface-2);
      font-size: var(--text-sm);
      color: var(--color-ink-2);
      overflow-wrap: anywhere;
    }
    .title {
      color: var(--color-ink);
      font-weight: var(--weight-semibold);
    }
    .link {
      margin-left: var(--space-1);
      @include bp.coarse {
        display: inline-flex;
        align-items: center;
        min-height: var(--touch-min);
      }
    }
  `,
})
export class DataPlanNote {
  readonly kind = input.required<PaidDataKind>();

  private readonly data = inject(DataCoverageService);
  private readonly session = inject(SessionService);

  protected readonly missing = computed(() => this.data.has(this.kind()) === false);
  protected readonly copy = computed(() => PLAN_COPY[this.kind()]);
  protected readonly isAdmin = computed(() => this.session.can('operations.run'));
}
