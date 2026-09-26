import { ChangeDetectionStrategy, Component, computed, inject } from '@angular/core';
import { RouterLink } from '@angular/router';

import { HaltStateService, haltScopeText } from '../../core/halts/halt-state.service';

/**
 * App-wide warning while a kill switch is on. The shell hosts it above the
 * page; it links to the halts page where the switch is resumed.
 */
@Component({
  selector: 'app-halt-banner',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink],
  template: `
    <div aria-live="polite">
      @if (kills().length) {
        <div class="banner" role="region" aria-label="Kill switch">
          <span class="mark" aria-hidden="true"></span>
          <p class="text">
            <strong>Kill switch on.</strong>
            {{ text() }}
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
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: var(--space-2) var(--space-3);
      margin-bottom: var(--space-4);
      padding: var(--space-3) var(--space-4);
      border: 1px solid var(--color-loss);
      border-left-width: 4px;
      border-radius: var(--radius-md);
      background: var(--color-loss-soft);
      color: var(--color-ink);
    }
    .mark {
      width: 10px;
      height: 10px;
      flex: none;
      background: var(--color-loss);
      transform: rotate(45deg);
    }
    .text {
      flex: 1 1 14rem;
      min-width: 0;
      margin: 0;
      overflow-wrap: anywhere;
    }
    strong {
      color: var(--color-loss);
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
  protected readonly kills = this.state.kills;

  protected readonly text = computed(() => {
    const kills = this.kills();
    const scopes = [...new Set(kills.map(haltScopeText))].join(', ');
    const allStopped = kills.some((k) => k.halt === 'all');
    const what = allStopped ? 'No new orders go out' : 'New buys are stopped, sells still go out';
    return `${scopes}. ${what} until someone resumes trading.`;
  });
}
