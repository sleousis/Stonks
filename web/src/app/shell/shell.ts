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
import {
  type ActivatedRouteSnapshot,
  ActivationEnd,
  NavigationEnd,
  Router,
  RouterLink,
  RouterOutlet,
} from '@angular/router';
import { filter, skip } from 'rxjs';

import type { Role } from '../api/models';
import { AuthTokenService } from '../core/auth/auth-token.service';
import { SessionService } from '../core/auth/session.service';
import { StepUpDialog } from '../core/auth/step-up-dialog';
import { ShortcutsService } from '../core/commands/shortcuts.service';
import { HaltStateService } from '../core/halts/halt-state.service';
import { ConnectivityService } from '../core/pwa/connectivity.service';
import { ThemeService } from '../core/theme/theme.service';
import { CommandPalette } from '../shared/ui/command-palette/command-palette';
import { ConfirmDialog } from '../shared/ui/confirm-dialog';
import { OfflinePage } from '../shared/ui/offline-page';
import { SessionStrip } from '../shared/ui/session-strip';
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
    StepUpDialog,
    // ops
    SessionStrip,
  ],
  templateUrl: './shell.html',
  styleUrl: './shell.scss',
  host: { '(document:keydown)': 'onKeydown($event)' },
})
export class Shell {
  protected readonly theme = inject(ThemeService);
  protected readonly auth = inject(AuthTokenService);
  protected readonly session = inject(SessionService);
  protected readonly shortcuts = inject(ShortcutsService);
  protected readonly connectivity = inject(ConnectivityService);
  private readonly router = inject(Router);
  private readonly injector = inject(Injector);
  private readonly drawer = viewChild.required<ElementRef<HTMLDialogElement>>('drawer');
  private readonly main = viewChild.required<ElementRef<HTMLElement>>('main');

  protected readonly drawerOpen = signal(false);
  protected readonly roleLabel: Readonly<Record<Role, string>> = {
    viewer: 'Viewer',
    trader: 'Trader',
    admin: 'Admin',
  };
  /** Pages with `data: { bare: true }` (sign-in) render without the app frame. */
  protected readonly bare = signal(false);
  protected readonly modKey = /Mac|iPhone|iPad/.test(globalThis.navigator?.platform ?? '')
    ? '⌘'
    : 'Ctrl';

  constructor() {
    registerShellCommands();
    // ops: keep the halt state in the session strip current
    inject(HaltStateService).watch(inject(DestroyRef));
    // The deepest route's data decides, before its component is created.
    this.router.events
      .pipe(
        filter((e) => e instanceof ActivationEnd && !e.snapshot.firstChild),
        takeUntilDestroyed(),
      )
      .subscribe((e) => this.bare.set(isBare((e as ActivationEnd).snapshot)));
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

  protected async signOut(): Promise<void> {
    this.closeDrawer();
    await this.session.logout();
    await this.router.navigateByUrl('/login');
  }

  private focusPage(): void {
    if (this.bare()) return;
    const main = this.main().nativeElement;
    const heading = main.querySelector<HTMLElement>('h1');
    (heading ?? main).focus({ preventScroll: false });
  }
}

/** True when the route or one of its parents has `data: { bare: true }`. */
function isBare(snapshot: ActivatedRouteSnapshot): boolean {
  return snapshot.pathFromRoot.some((r) => r.data['bare'] === true);
}
