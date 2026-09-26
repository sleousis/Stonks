import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  computed,
  inject,
  input,
  output,
  signal,
} from '@angular/core';

/** How long the button must be held, in milliseconds. */
export const HOLD_MS = 1000;
/** How long a first press through a screen reader stays armed. */
export const ARM_MS = 5000;

let nextId = 0;

/**
 * A confirm button that must be held for about a second: press and hold
 * with a mouse or finger, or hold Enter or Space. A fill shows the progress
 * and letting go early cancels.
 *
 * Accessible fallback: a screen reader activates a button with a plain click
 * and no press, so such a click arms the button ("Press again to confirm")
 * and a second one within a few seconds confirms.
 *
 *   <app-hold-button label="Go live" [disabled]="!ok()" (confirmed)="send()" />
 */
@Component({
  selector: 'app-hold-button',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <button
      type="button"
      class="btn hold"
      [class.btn-primary]="tone() !== 'danger'"
      [class.btn-danger]="tone() === 'danger'"
      [class.holding]="holding()"
      [style.--hold-ms]="holdMs() + 'ms'"
      [disabled]="disabled()"
      [attr.aria-describedby]="hintId"
      (pointerdown)="onPointerDown($event)"
      (pointerup)="release()"
      (pointerleave)="release()"
      (pointercancel)="release()"
      (contextmenu)="$event.preventDefault()"
      (keydown)="onKeyDown($event)"
      (keyup)="onKeyUp($event)"
      (click)="onClick($event)"
    >
      <span class="fill" aria-hidden="true"></span>
      <span class="text">{{ text() }}</span>
    </button>
    <span class="visually-hidden" [id]="hintId">
      Press and hold for one second, or hold Enter or Space. With a screen reader, press twice.
    </span>
  `,
  styles: `
    :host {
      display: inline-flex;
    }
    .hold {
      position: relative;
      overflow: hidden;
      width: 100%;
      touch-action: none;
      user-select: none;
      -webkit-user-select: none;
      -webkit-touch-callout: none;
    }
    .fill {
      position: absolute;
      inset: 0;
      background: currentColor;
      opacity: 0.22;
      transform: scaleX(0);
      transform-origin: left center;
    }
    .holding .fill {
      transform: scaleX(1);
      transition: transform var(--hold-ms) linear;
    }
    .text {
      position: relative;
    }
  `,
})
export class HoldButton {
  /** The action's verb ("Go live"). */
  readonly label = input.required<string>();
  readonly tone = input<'default' | 'danger'>('default');
  readonly disabled = input(false);
  readonly holdMs = input(HOLD_MS);
  readonly confirmed = output<void>();

  protected readonly hintId = `hold-hint-${nextId++}`;
  protected readonly holding = signal(false);
  protected readonly armed = signal(false);
  private readonly heldTooShort = signal(false);

  private holdTimer: ReturnType<typeof setTimeout> | null = null;
  private armTimer: ReturnType<typeof setTimeout> | null = null;

  protected readonly text = computed(() => {
    if (this.holding()) return 'Keep holding…';
    if (this.armed()) return 'Press again to confirm';
    if (this.heldTooShort()) return 'Hold a little longer';
    return `Hold to ${this.label().toLowerCase()}`;
  });

  constructor() {
    inject(DestroyRef).onDestroy(() => {
      this.clearHold();
      this.disarm();
    });
  }

  protected onPointerDown(event: PointerEvent): void {
    if (this.disabled() || event.button !== 0) return;
    this.start();
  }

  protected onKeyDown(event: KeyboardEvent): void {
    if (event.key !== 'Enter' && event.key !== ' ') return;
    // No synthetic click: the hold decides.
    event.preventDefault();
    if (event.repeat || this.disabled()) return;
    this.start();
  }

  protected onKeyUp(event: KeyboardEvent): void {
    if (event.key !== 'Enter' && event.key !== ' ') return;
    event.preventDefault();
    this.release();
  }

  protected release(): void {
    if (!this.holding()) return;
    this.clearHold();
    this.heldTooShort.set(true);
  }

  protected onClick(event: MouseEvent): void {
    // A mouse or touch click (detail >= 1) follows a press: the hold decides.
    // Keys never click (their default is prevented). What is left is a click
    // with no press, from assistive technology: two of them confirm.
    if (this.disabled() || event.detail !== 0) return;
    if (this.armed()) {
      this.disarm();
      this.confirmed.emit();
      return;
    }
    this.armed.set(true);
    this.armTimer = setTimeout(() => this.disarm(), ARM_MS);
  }

  private start(): void {
    this.clearHold();
    this.disarm();
    this.heldTooShort.set(false);
    this.holding.set(true);
    this.holdTimer = setTimeout(() => {
      this.holdTimer = null;
      this.holding.set(false);
      this.heldTooShort.set(false);
      this.confirmed.emit();
    }, this.holdMs());
  }

  private clearHold(): void {
    if (this.holdTimer) clearTimeout(this.holdTimer);
    this.holdTimer = null;
    this.holding.set(false);
  }

  private disarm(): void {
    if (this.armTimer) clearTimeout(this.armTimer);
    this.armTimer = null;
    this.armed.set(false);
  }
}
