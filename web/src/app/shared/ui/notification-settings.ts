import { ChangeDetectionStrategy, Component, OnInit, computed, inject } from '@angular/core';

import { NotificationPermissionService } from '../../core/pwa/notification-permission.service';
import { StatusPill } from './status-pill';

/**
 * Settings panel: opt in to notifications on this device. The browser's
 * permission prompt appears only after the trader presses the button.
 */
@Component({
  selector: 'app-notification-settings',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [StatusPill],
  template: `
    <section class="panel" aria-labelledby="notify-title">
      <div class="panel-head">
        <h3 id="notify-title">Notifications</h3>
        <app-status-pill [status]="pill().status" [tone]="pill().tone" [label]="pill().label" />
      </div>
      <div class="panel-body">
        <p class="lead">
          Get signals, fills and risk alerts on this device, even when the console is closed.
          Messages carry no amounts or holdings; details open in the console.
        </p>

        @switch (svc.state()) {
          @case ('unsupported') {
            <p class="note">This browser cannot show notifications. Try Chrome, Edge or Firefox.</p>
          }
          @case ('install-first') {
            <p class="note">
              On iPhone and iPad, notifications only reach the installed app. In Safari, tap Share,
              then <strong>Add to Home Screen</strong>, open Stonks from there and come back here.
            </p>
          }
          @case ('denied') {
            <p class="note">
              Notifications are blocked for this site. Allow them in your browser's site settings,
              then reload this page.
            </p>
          }
          @default {
            @switch (svc.push()) {
              @case ('on') {
                <p class="note ok">This device gets notifications.</p>
                <button type="button" class="btn" [disabled]="svc.busy()" (click)="svc.disable()">
                  Turn off on this device
                </button>
              }
              @case ('waiting-for-server') {
                <p class="note">
                  Allowed. Delivery starts once the server supports push; nothing else to do here.
                </p>
              }
              @case ('no-worker') {
                <p class="note">
                  Allowed. Delivery needs the installed console (production build), not the dev
                  server.
                </p>
              }
              @default {
                <button
                  type="button"
                  class="btn btn-primary"
                  [disabled]="svc.busy()"
                  [attr.aria-busy]="svc.busy()"
                  (click)="svc.enable()"
                >
                  {{ svc.busy() ? 'Asking…' : 'Turn on notifications' }}
                </button>
                <p class="hint">Your browser will ask to confirm.</p>
              }
            }
          }
        }
        @if (svc.error(); as err) {
          <p class="note error" role="alert">{{ err }}</p>
        }
      </div>
    </section>
  `,
  styles: `
    :host {
      display: block;
    }
    .panel-body {
      display: grid;
      justify-items: start;
      gap: var(--space-3);
    }
    .lead {
      color: var(--color-ink-2);
      max-width: 70ch;
    }
    .note {
      padding: var(--space-2) var(--space-3);
      border-left: 3px solid var(--color-info);
      border-radius: var(--radius-sm);
      background: var(--color-info-soft);
      font-size: var(--text-sm);
    }
    .note.ok {
      border-left-color: var(--color-gain);
      background: var(--color-gain-soft);
    }
    .note.error {
      border-left-color: var(--color-loss);
      background: var(--color-loss-soft);
    }
    .hint {
      margin-top: calc(-1 * var(--space-2));
      font-size: var(--text-xs);
      color: var(--color-ink-3);
    }
  `,
})
export class NotificationSettings implements OnInit {
  protected readonly svc = inject(NotificationPermissionService);

  protected readonly pill = computed(() => {
    const state = this.svc.state();
    if (state === 'granted' && this.svc.push() === 'on') {
      return { status: 'ok', tone: 'positive' as const, label: 'On' };
    }
    if (state === 'granted') return { status: 'info', tone: 'info' as const, label: 'Allowed' };
    if (state === 'denied') return { status: 'error', tone: 'negative' as const, label: 'Blocked' };
    return { status: 'disabled', tone: 'neutral' as const, label: 'Off' };
  });

  ngOnInit(): void {
    void this.svc.refresh();
  }
}
