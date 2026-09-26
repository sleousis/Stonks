import {
  ChangeDetectionStrategy,
  Component,
  OnInit,
  computed,
  inject,
  resource,
  signal,
} from '@angular/core';
import { RouterLink } from '@angular/router';
import { SwPush } from '@angular/service-worker';
import { firstValueFrom } from 'rxjs';

import type { PushDeviceView } from '../../api/models';
import { NotificationsService } from '../../api/notifications.service';
import { isMissingRoute } from '../../api/subscriptions.service';
import { SessionService } from '../../core/auth/session.service';
import { ConfirmService } from '../../core/confirm/confirm.service';
import { formatAgo, formatDate } from '../../core/format/format';
import { ToastService } from '../../core/notify/toast.service';
import { NotificationPermissionService } from '../../core/pwa/notification-permission.service';
import { PermissionNote } from '../../shared/ui/permission-note';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import { StatusPill } from '../../shared/ui/status-pill';

const BROWSERS: readonly [RegExp, string][] = [
  [/Edg(A|iOS)?\//, 'Edge'],
  [/OPR\/|Opera/, 'Opera'],
  [/SamsungBrowser\//, 'Samsung Internet'],
  [/Firefox\/|FxiOS\//, 'Firefox'],
  [/Chrome\/|CriOS\//, 'Chrome'],
  [/Safari\//, 'Safari'],
];

const SYSTEMS: readonly [RegExp, string][] = [
  [/iPhone/, 'iPhone'],
  [/iPad/, 'iPad'],
  [/Android/, 'Android'],
  [/CrOS/, 'ChromeOS'],
  [/Windows/, 'Windows'],
  [/Mac OS X|Macintosh/, 'Mac'],
  [/Linux/, 'Linux'],
];

/** "Chrome on Windows" from a user agent, or null when it says nothing useful. */
export function deviceName(userAgent: string | null | undefined): string | null {
  if (!userAgent) return null;
  const browser = BROWSERS.find(([re]) => re.test(userAgent))?.[1];
  const system = SYSTEMS.find(([re]) => re.test(userAgent))?.[1];
  if (browser && system) return `${browser} on ${system}`;
  return browser ?? system ?? null;
}

/** Is this device row the browser we are running in? */
export function isThisBrowser(
  device: Pick<PushDeviceView, 'endpoint_host' | 'user_agent'>,
  endpoint: string | null,
  userAgent: string,
): boolean {
  if (!endpoint) return false;
  try {
    return new URL(endpoint).hostname === device.endpoint_host && device.user_agent === userAgent;
  } catch {
    return false;
  }
}

/**
 * The browsers and installed apps that get push notifications for this
 * trader. Removing this browser stops its subscription too. Other devices
 * are removed on the server only.
 */
@Component({
  selector: 'app-push-devices',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, StatusPill, PermissionNote, LoadingState, EmptyState, ErrorState],
  template: `
    <section class="panel" aria-labelledby="devices-title">
      <div class="panel-head">
        <h2 id="devices-title">Your devices</h2>
      </div>
      <p class="lead">
        These browsers and apps get push notifications. To turn push on in this browser, go to
        <a routerLink="/settings">Settings</a>.
      </p>
      @if (devices.error(); as err) {
        <app-error-state title="Could not load devices" [error]="err" (retry)="devices.reload()" />
      } @else if (!devices.hasValue()) {
        <app-loading-state label="Loading devices" [rows]="2" />
      } @else if (devices.value().length === 0) {
        <app-empty-state
          title="No devices yet"
          message="Turn on notifications in Settings on each phone or computer you use. It shows here once it is set up."
        >
          <a class="btn" routerLink="/settings">Open Settings</a>
        </app-empty-state>
      } @else {
        <ul class="devices">
          @for (d of devices.value(); track d.id) {
            <li>
              <div class="text">
                <p class="name">
                  {{ name(d) }}
                  @if (isThis(d)) {
                    <span class="this muted">This browser</span>
                  }
                </p>
                <p class="facts muted">
                  Added {{ day(d.created_at) }}
                  @if (d.last_success_at) {
                    <span> · Last used {{ ago(d.last_success_at) }}</span>
                  } @else {
                    <span> · Not used yet</span>
                  }
                </p>
                @if (d.failure_count > 0) {
                  <app-status-pill status="warning" label="Recent deliveries failed" />
                }
              </div>
              <button
                type="button"
                class="btn btn-ghost"
                [disabled]="removing() === d.id || !canManage()"
                (click)="remove(d)"
              >
                Remove<span class="visually-hidden">: {{ name(d) }}</span>
              </button>
            </li>
          }
        </ul>
        <div class="note-row"><app-permission-note permission="notifications.manage" /></div>
      }
    </section>
  `,
  styles: `
    :host {
      display: block;
      min-width: 0;
    }
    .lead {
      padding: var(--space-3) var(--space-4) 0;
      color: var(--color-ink-2);
      font-size: var(--text-sm);
    }
    .devices {
      margin: 0;
      padding: 0;
      list-style: none;
    }
    li {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: var(--space-2) var(--space-3);
      padding: var(--space-3) var(--space-4);
      border-bottom: 1px solid var(--color-border);
    }
    .text {
      display: grid;
      justify-items: start;
      gap: var(--space-1);
      min-width: 0;
      flex: 1 1 14rem;
    }
    .name {
      font-weight: var(--weight-semibold);
      overflow-wrap: anywhere;
    }
    .this {
      margin-left: var(--space-2);
      font-size: var(--text-xs);
      font-weight: var(--weight-regular);
    }
    .facts {
      font-size: var(--text-xs);
    }
    .note-row {
      padding: 0 var(--space-4) var(--space-3);
    }
  `,
})
export class PushDevices implements OnInit {
  private readonly api = inject(NotificationsService);
  private readonly confirm = inject(ConfirmService);
  private readonly toasts = inject(ToastService);
  private readonly session = inject(SessionService);
  private readonly permission = inject(NotificationPermissionService);
  private readonly swPush = inject(SwPush, { optional: true });

  protected readonly devices = resource({ loader: () => this.api.pushDevices() });
  protected readonly canManage = computed(() => this.session.can('notifications.manage'));
  protected readonly removing = signal<string | null>(null);
  /** This browser's push endpoint, when it is subscribed. */
  private readonly endpoint = signal<string | null>(null);

  async ngOnInit(): Promise<void> {
    if (!this.swPush?.isEnabled) return;
    try {
      const sub = await firstValueFrom(this.swPush.subscription);
      this.endpoint.set(sub?.endpoint ?? null);
    } catch {
      // No subscription to compare with: no row is marked as this browser.
    }
  }

  protected name(d: PushDeviceView): string {
    return deviceName(d.user_agent) ?? `Device using ${d.endpoint_host}`;
  }

  protected isThis(d: PushDeviceView): boolean {
    return isThisBrowser(d, this.endpoint(), navigator.userAgent);
  }

  protected day(value: string): string {
    return formatDate(value);
  }

  protected ago(value: string): string {
    return formatAgo(value);
  }

  protected async remove(d: PushDeviceView): Promise<void> {
    const name = this.name(d);
    const self = this.isThis(d);
    const ok = await this.confirm.confirm({
      title: `Remove ${name}?`,
      message: self
        ? 'This browser stops getting push notifications. You can turn them on again in Settings.'
        : 'That device stops getting push notifications. Everything still shows here in the feed.',
      confirmLabel: 'Remove device',
      tone: 'danger',
    });
    if (!ok) return;
    this.removing.set(d.id);
    try {
      if (self) {
        await this.permission.disable();
        const err = this.permission.error();
        if (err) {
          this.toasts.error(err);
          return;
        }
        this.endpoint.set(null);
      } else {
        await this.api.removePushDevice(d.id);
      }
      this.toasts.success(`Removed ${name}.`);
      this.devices.reload();
    } catch (err) {
      if (isMissingRoute(err)) {
        this.toasts.info(
          'Removing another device from here is coming soon. For now, turn notifications off in Settings on that device.',
        );
      } else {
        this.toasts.error(err instanceof Error ? err.message : 'Could not remove the device.');
      }
    } finally {
      this.removing.set(null);
    }
  }
}
