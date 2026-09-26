import { DOCUMENT, NgTemplateOutlet } from '@angular/common';
import {
  ChangeDetectionStrategy,
  Component,
  type ElementRef,
  Injector,
  afterNextRender,
  inject,
  signal,
  viewChild,
} from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { NavigationEnd, Router, RouterLink, RouterOutlet } from '@angular/router';
import { filter, skip } from 'rxjs';

import { AuthTokenService } from '../core/auth/auth-token.service';
import { ThemeService } from '../core/theme/theme.service';
import { ConfirmDialog } from '../shared/ui/confirm-dialog';
import { ToastOutlet } from '../shared/ui/toast-outlet';
import { Nav } from './nav';
import { NAV_ITEMS } from './nav-items';

const SHORTCUT_WINDOW_MS = 1200;

/**
 * App frame: sidebar navigation from tablet width up; on phones a top bar
 * with a menu button that opens the navigation in a drawer. Also hosts the
 * confirm dialog and toasts, "g then key" shortcuts, and moves focus to the
 * page heading after each navigation.
 */
@Component({
  selector: 'app-shell',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgTemplateOutlet, RouterOutlet, RouterLink, Nav, ConfirmDialog, ToastOutlet],
  templateUrl: './shell.html',
  styleUrl: './shell.scss',
  host: { '(document:keydown)': 'onKeydown($event)' },
})
export class Shell {
  protected readonly theme = inject(ThemeService);
  protected readonly auth = inject(AuthTokenService);
  private readonly router = inject(Router);
  private readonly doc = inject(DOCUMENT);
  private readonly injector = inject(Injector);
  private readonly drawer = viewChild.required<ElementRef<HTMLDialogElement>>('drawer');
  private readonly main = viewChild.required<ElementRef<HTMLElement>>('main');

  protected readonly drawerOpen = signal(false);
  private pendingG = 0;

  constructor() {
    this.router.events
      .pipe(
        filter((e) => e instanceof NavigationEnd),
        skip(1),
        takeUntilDestroyed(),
      )
      .subscribe(() => {
        this.closeDrawer();
        afterNextRender(() => this.focusPage(), { injector: this.injector });
      });
  }

  protected openDrawer(): void {
    const el = this.drawer().nativeElement;
    if (!el.open) el.showModal?.();
    this.drawerOpen.set(true);
  }

  protected closeDrawer(): void {
    const el = this.drawer().nativeElement;
    if (el.open) el.close();
    this.drawerOpen.set(false);
  }

  /** Close when the backdrop (the dialog element itself) is clicked. */
  protected onDrawerClick(event: MouseEvent): void {
    if (event.target === this.drawer().nativeElement) this.closeDrawer();
  }

  protected onKeydown(event: KeyboardEvent): void {
    if (event.defaultPrevented || event.ctrlKey || event.metaKey || event.altKey) return;
    if (isTyping(event.target) || this.doc.querySelector('dialog[open]')) return;
    const now = Date.now();
    if (this.pendingG && now - this.pendingG < SHORTCUT_WINDOW_MS) {
      this.pendingG = 0;
      const item = NAV_ITEMS.find((i) => i.key === event.key.toLowerCase());
      if (item) {
        event.preventDefault();
        void this.router.navigateByUrl(item.path);
      }
      return;
    }
    if (event.key === 'g') this.pendingG = now;
  }

  private focusPage(): void {
    const main = this.main().nativeElement;
    const heading = main.querySelector<HTMLElement>('h1');
    (heading ?? main).focus({ preventScroll: false });
  }
}

function isTyping(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  return (
    target.isContentEditable ||
    target instanceof HTMLInputElement ||
    target instanceof HTMLTextAreaElement ||
    target instanceof HTMLSelectElement
  );
}
