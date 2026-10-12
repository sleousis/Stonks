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

import { AuthTokenService } from '../core/auth/auth-token.service';
import { SessionService } from '../core/auth/session.service';
import { StepUpDialog } from '../core/auth/step-up-dialog';
import { StepUpService } from '../core/auth/step-up.service';
import { ShortcutsService } from '../core/commands/shortcuts.service';
import { HaltStateService } from '../core/halts/halt-state.service';
import { ConnectivityService } from '../core/pwa/connectivity.service';
import { ThemeService } from '../core/theme/theme.service';
import { NotificationBell } from '../shared/ui/notification-bell';
import { CommandPalette } from '../shared/ui/command-palette/command-palette';
import { ConfirmDialog } from '../shared/ui/confirm-dialog';
import { OfflinePage } from '../shared/ui/offline-page';
import { SessionStrip } from '../shared/ui/session-strip';
import { ShortcutHelp } from '../shared/ui/shortcut-help';
import { ToastOutlet } from '../shared/ui/toast-outlet';
import { AccountMenu } from './account-menu';
import { BottomNav } from './bottom-nav';
import { registerShellCommands } from './shell-commands';
import { WorkspaceMenu } from './workspace-menu';
import { WorkspaceTabs } from './workspace-tabs';

/**
 * App frame: an ink chrome bar across the top (the brand, the workspace
 * switcher, the workspace's page tabs, search, the bell, the theme and the
 * account menu), the session rail under it, and on phones a bottom nav with
 * a More sheet. Also hosts the confirm dialog, toasts, the command palette
 * (Ctrl+K) and the shortcut cheat sheet (?), forwards key presses to
 * ShortcutsService, and moves focus to the page heading after each
 * navigation. The palette, the cheat sheet and the step-up prompt load on
 * first use (`@defer`), so the first screen ships less code. The rail turns
 * red while a kill switch is on.
 */
@Component({
  selector: 'app-shell',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    RouterOutlet,
    RouterLink,
    WorkspaceMenu,
    WorkspaceTabs,
    BottomNav,
    AccountMenu,
    ConfirmDialog,
    ToastOutlet,
    CommandPalette,
    ShortcutHelp,
    OfflinePage,
    StepUpDialog,
    // ops
    SessionStrip,
    NotificationBell,
  ],
  templateUrl: './shell.html',
  styleUrl: './shell.scss',
  host: {
    '(document:keydown)': 'onKeydown($event)',
    '[class.has-bottom-nav]': '!bare()',
  },
})
export class Shell {
  protected readonly theme = inject(ThemeService);
  protected readonly auth = inject(AuthTokenService);
  protected readonly session = inject(SessionService);
  /** The halt state: the rail turns red while a kill switch is on (UX-51). */
  protected readonly halts = inject(HaltStateService);
  /** The step-up prompt's requests: the dialog loads on the first one (UX-44). */
  protected readonly stepUp = inject(StepUpService);
  protected readonly shortcuts = inject(ShortcutsService);
  protected readonly connectivity = inject(ConnectivityService);
  private readonly router = inject(Router);
  private readonly injector = inject(Injector);
  private readonly main = viewChild<ElementRef<HTMLElement>>('main');

  /** Pages with `data: { bare: true }` (sign-in) render without the app frame. */
  protected readonly bare = signal(false);
  protected readonly modKey = /Mac|iPhone|iPad/.test(globalThis.navigator?.platform ?? '')
    ? '⌘'
    : 'Ctrl';

  constructor() {
    registerShellCommands();
    // ops: keep the halt state in the session rail current
    this.halts.watch(inject(DestroyRef));
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
        afterNextRender(() => this.focusPage(), { injector: this.injector });
      });
  }

  protected onKeydown(event: KeyboardEvent): void {
    this.shortcuts.handle(event);
  }

  protected openPalette(): void {
    this.shortcuts.openPalette();
  }

  protected async signOut(): Promise<void> {
    // logout() loads /login afresh, so no user's state outlives the session (UX-07).
    await this.session.logout();
  }

  private focusPage(): void {
    if (this.bare()) return;
    const main = this.main()?.nativeElement;
    if (!main) return;
    const heading = main.querySelector<HTMLElement>('h1');
    (heading ?? main).focus({ preventScroll: false });
  }
}

/** True when the route or one of its parents has `data: { bare: true }`. */
function isBare(snapshot: ActivatedRouteSnapshot): boolean {
  return snapshot.pathFromRoot.some((r) => r.data['bare'] === true);
}
