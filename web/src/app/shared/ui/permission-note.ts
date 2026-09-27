import { ChangeDetectionStrategy, Component, computed, inject, input } from '@angular/core';

import { SessionService } from '../../core/auth/session.service';
import type { Permission } from '../../core/auth/permissions';

/**
 * A one-line reason next to an action the current user may not take
 * ("Admins only."). Renders nothing when the user has the permission, so a
 * page can place it right after the disabled or hidden control:
 *
 *   <button class="btn" [disabled]="!session.can('operations.run')">Run tick</button>
 *   <app-permission-note permission="operations.run" />
 */
@Component({
  selector: 'app-permission-note',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @if (reason(); as r) {
      <p class="permission-note">
        <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
          <rect x="3.5" y="7" width="9" height="6.5" rx="1" fill="none" stroke="currentColor" />
          <path d="M5.5 7V5a2.5 2.5 0 0 1 5 0v2" fill="none" stroke="currentColor" />
        </svg>
        <span>{{ r }}</span>
      </p>
    }
  `,
  styles: `
    :host {
      display: contents;
    }
    .permission-note {
      display: flex;
      align-items: center;
      gap: var(--space-1);
      margin: var(--space-1) 0 0;
      font-size: var(--text-xs);
      color: var(--color-ink-3);
    }
    svg {
      flex: none;
    }
  `,
})
export class PermissionNote {
  readonly permission = input.required<Permission>();
  private readonly session = inject(SessionService);
  protected readonly reason = computed(() => this.session.whyNot(this.permission()));
}
