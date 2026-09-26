import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import { qrModules, qrPath } from '../../core/auth/qr';

/** A QR code drawn as one SVG path, dark on white in both themes (scanners need contrast). */
@Component({
  selector: 'app-qr-code',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <svg
      role="img"
      [attr.aria-label]="label()"
      [attr.viewBox]="viewBox()"
      shape-rendering="crispEdges"
    >
      <rect [attr.x]="-quiet" [attr.y]="-quiet" width="100%" height="100%" fill="#fff" />
      <path [attr.d]="path()" fill="#000" />
    </svg>
  `,
  styles: `
    :host {
      display: block;
      width: min(100%, 15rem);
    }
    svg {
      display: block;
      width: 100%;
      height: auto;
      border-radius: var(--radius-sm);
      background: #fff;
    }
  `,
})
export class QrCode {
  readonly value = input.required<string>();
  readonly label = input('QR code');

  /** Four modules of white around the code, as scanners expect. */
  protected readonly quiet = 4;
  private readonly modules = computed(() => qrModules(this.value()));
  protected readonly path = computed(() => qrPath(this.modules()));
  protected readonly viewBox = computed(() => {
    const size = this.modules().length + 2 * this.quiet;
    return `${-this.quiet} ${-this.quiet} ${size} ${size}`;
  });
}
