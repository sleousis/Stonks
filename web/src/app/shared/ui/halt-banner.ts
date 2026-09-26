import { ChangeDetectionStrategy, Component, computed, inject } from '@angular/core';
import { RouterLink } from '@angular/router';

import type { HaltView } from '../../api/models';
import { HaltStateService, haltScopeText } from '../../core/halts/halt-state.service';

const KIND_TEXT: Record<HaltView['kind'], string> = {
  kill: 'kill switch',
  month_loss: 'monthly loss breaker',
  week_loss: 'weekly loss breaker',
  drawdown: 'drawdown breaker',
  operational: 'operational halt',
};

interface BannerView {
  tone: 'kill' | 'halt';
  title: string;
  text: string;
}

/** What the banner says for the active halts, or null when trading is not halted. */
export function bannerView(active: readonly HaltView[]): BannerView | null {
  const halts = active.filter((h) => h.active);
  if (!halts.length) return null;
  const kills = halts.filter((h) => h.kind === 'kill');
  const shown = kills.length ? kills : halts;
  const scopes = [...new Set(shown.map(haltScopeText))].join(', ');
  const what = halts.some((h) => h.halt === 'all')
    ? 'No new orders go out'
    : 'New buys are stopped, sells still go out';
  if (kills.length) {
    return {
      tone: 'kill',
      title: 'Kill switch on.',
      text: `${scopes}. ${what} until someone resumes trading.`,
    };
  }
  const kinds = [...new Set(halts.map((h) => KIND_TEXT[h.kind]))].join(', ');
  const title = halts.length === 1 ? 'Trading halted.' : `${halts.length} halts active.`;
  return { tone: 'halt', title, text: `${scopes}: ${kinds}. ${what} until it is cleared.` };
}

/**
 * App-wide warning while any halt is active: red for a kill switch, amber
 * for a circuit breaker or operational halt. The shell hosts it above the
 * page, and it links to the halts page where halts are resumed or cleared.
 */
@Component({
  selector: 'app-halt-banner',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink],
  template: `
    <div aria-live="polite">
      @if (view(); as v) {
        <div class="banner" [attr.data-tone]="v.tone" role="region" aria-label="Trading halted">
          <span class="mark" aria-hidden="true"></span>
          <p class="text">
            <strong>{{ v.title }}</strong>
            {{ v.text }}
          </p>
          <a class="btn" routerLink="/ops/halts">Review halts</a>
        </div>
      }
    </div>
  `,
  styles: `
    @use 'breakpoints' as bp;
    :host {
      display: block;
    }
    .banner {
      --tone: var(--color-loss);
      --tone-soft: var(--color-loss-soft);
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: var(--space-2) var(--space-3);
      margin-bottom: var(--space-4);
      padding: var(--space-3) var(--space-4);
      border: 1px solid var(--tone);
      border-left-width: 4px;
      border-radius: var(--radius-md);
      background: var(--tone-soft);
      color: var(--color-ink);
    }
    .banner[data-tone='halt'] {
      --tone: var(--color-warn);
      --tone-soft: var(--color-warn-soft);
    }
    .mark {
      width: 10px;
      height: 10px;
      flex: none;
      background: var(--tone);
      transform: rotate(45deg);
    }
    .banner[data-tone='halt'] .mark {
      transform: none;
      clip-path: polygon(50% 0, 100% 100%, 0 100%);
      width: 12px;
    }
    .text {
      flex: 1 1 14rem;
      min-width: 0;
      margin: 0;
      overflow-wrap: anywhere;
    }
    strong {
      color: var(--tone);
    }
    @include bp.phone {
      .btn {
        width: 100%;
      }
    }
  `,
})
export class HaltBanner {
  private readonly state = inject(HaltStateService);
  protected readonly view = computed(() => bannerView(this.state.active()));
}
