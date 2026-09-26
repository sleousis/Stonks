import { NgTemplateOutlet } from '@angular/common';
import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
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
import { ShortcutsService } from '../core/commands/shortcuts.service';
import { HaltStateService } from '../core/halts/halt-state.service';
import { ConnectivityService } from '../core/pwa/connectivity.service';
import { ThemeService } from '../core/theme/theme.service';
import { CommandPalette } from '../shared/ui/command-palette/command-palette';
import { ConfirmDialog } from '../shared/ui/confirm-dialog';
import { HaltBanner } from '../shared/ui/halt-banner';
import { OfflinePage } from '../shared/ui/offline-page';
import { ShortcutHelp } from '../shared/ui/shortcut-help';
import { ToastOutlet } from '../shared/ui/toast-outlet';
import { Nav } from './nav';
import { registerShellCommands } from './shell-commands';

/**
 * App frame: sidebar navigation from tablet width up; on phones a top bar
 * with a menu button that opens the navigation in a drawer. Also hosts the
 * confirm dialog, toasts, the command palette (Ctrl+K) and the shortcut cheat
 * sheet (?), forwards key presses to ShortcutsService, and moves focus to
 * the page heading after each navigation.
 */
@Component({
  selector: 'app-shell',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    NgTemplateOutlet,
    RouterOutlet,
    RouterLink,
    Nav,
    ConfirmDialog,
    ToastOutlet,
    CommandPalette,
    ShortcutHelp,
    OfflinePage,
    // ops
    HaltBanner,
  ],
  templateUrl: './shell.html',
  styleUrl: './shell.scss',
  host: { '(document:keydown)': 'onKeydown($event)' },
})
export class Shell {
  protected readonly theme = inject(ThemeService);
  protected readonly auth = inject(AuthTokenService);
  protected readonly shortcuts = inject(ShortcutsService);
  protected readonly connectivity = inject(ConnectivityService);
  private readonly router = inject(Router);
  private readonly injector = inject(Injector);
  private readonly drawer = viewChild.required<ElementRef<HTMLDialogElement>>('drawer');
  private readonly main = viewChild.required<ElementRef<HTMLElement>>('main');

  protected readonly drawerOpen = signal(false);
  protected readonly modKey = /Mac|iPhone|iPad/.test(globalThis.navigator?.platform ?? '')
    ? '⌘'
    : 'Ctrl';

  constructor() {
    registerShellCommands();
    // ops: keep the kill switch banner current
    inject(HaltStateService).watch(inject(DestroyRef));
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
    this.shortcuts.handle(event);
  }

  protected openPalette(): void {
    this.closeDrawer();
    this.shortcuts.openPalette();
  }

  private focusPage(): void {
    const main = this.main().nativeElement;
    const heading = main.querySelector<HTMLElement>('h1');
    (heading ?? main).focus({ preventScroll: false });
  }
}
