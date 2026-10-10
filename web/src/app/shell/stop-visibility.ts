import { Injectable, computed, effect, inject, signal, untracked } from '@angular/core';

import { SubscriptionsService } from '../api/subscriptions.service';
import { SessionService } from '../core/auth/session.service';
import { PortfolioContextService } from '../core/portfolio/portfolio-context.service';

const SHOWN_KEY = 'stonks.stopShown.';

/**
 * Whether the Stop trading control and its palette command show (FR-06,
 * NB-15). A new trader with nothing that trades does not need the biggest
 * red button on every page. It shows once the person has something to
 * stop: a broker portfolio, or a follow that trades (Paper, Approve each
 * trade or Automatic). Admins always see it. Once shown it stays, on this
 * device, so it never disappears under someone who learned where it is.
 */
@Injectable({ providedIn: 'root' })
export class StopTradingVisibility {
  private readonly session = inject(SessionService);
  private readonly portfolios = inject(PortfolioContextService);
  private readonly subscriptions = inject(SubscriptionsService);

  /** Shown before on this device, for this person. */
  private readonly sticky = signal(false);
  /** A follow that trades, from the subscriptions read. */
  private readonly trading = signal(false);
  private checkedFor: string | null = null;

  private readonly brokerBook = computed(() =>
    this.portfolios.options().some((p) => p.kind === 'broker' || p.trading === 'live'),
  );

  /** Whether the control shows. Reactive. */
  readonly show = computed(() => {
    // Nobody signed in: as before, the permission alone decides.
    if (this.session.me() === null) return this.session.can('killswitch.user');
    if (!this.session.can('killswitch.user')) return false;
    if (this.session.isAdmin()) return true;
    return this.sticky() || this.brokerBook() || this.trading();
  });

  constructor() {
    // Each person starts from what this device remembers for them.
    effect(() => {
      const id = this.userId();
      untracked(() => {
        this.sticky.set(id ? readShown(id) : false);
        this.trading.set(false);
        this.checkedFor = null;
      });
    });
    // A follow that trades needs a portfolio, so read the follows only once one exists.
    effect(() => {
      const id = this.userId();
      const books = this.portfolios.options().length;
      if (!id || !books || this.sticky() || this.brokerBook()) return;
      untracked(() => {
        if (this.checkedFor === id) return;
        this.checkedFor = id;
        void this.check();
      });
    });
    // Once it shows, it stays.
    effect(() => {
      const id = this.userId();
      if (id && this.show() && !this.sticky()) untracked(() => this.remember(id));
    });
  }

  private userId(): string | null {
    const me = this.session.me() as { user_id?: string } | null;
    return me?.user_id ?? null;
  }

  private async check(): Promise<void> {
    try {
      const subs = await this.subscriptions.list();
      this.trading.set(subs.some((s) => s.enabled && s.mode !== 'notify'));
    } catch {
      // Unknown: leave it hidden until the next sign-in.
    }
  }

  private remember(id: string): void {
    this.sticky.set(true);
    try {
      localStorage.setItem(SHOWN_KEY + id, '1');
    } catch {
      // Storage blocked: it shows for this page view.
    }
  }
}

function readShown(id: string): boolean {
  try {
    return localStorage.getItem(SHOWN_KEY + id) === '1';
  } catch {
    return false;
  }
}
