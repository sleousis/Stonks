import { DOCUMENT } from '@angular/common';
import { Injectable, inject, signal } from '@angular/core';
import { Router } from '@angular/router';

import { CommandRegistry } from './command-registry';

/** A two-key sequence: press `prefix`, then `key`, within a second or so. */
export interface KeySequence {
  prefix: 'g' | 'n';
  key: string;
  label: string;
  /** Navigate to this path... */
  path?: string;
  /** ...or run the palette command with this id. */
  commandId?: string;
}

export const SEQUENCE_WINDOW_MS = 1200;
export const SHORTCUTS_STORAGE_KEY = 'stonks.shortcuts';

/**
 * Global keyboard shortcuts. The shell forwards `document:keydown` here.
 *
 * - Ctrl+K / Cmd+K toggles the command palette, even while typing.
 * - `/` opens the palette, `?` shows the cheat sheet.
 * - `g` then a key jumps to a page; `n` then a key starts something new.
 *
 * Single-character shortcuts can be turned off in the cheat sheet (WCAG
 * 2.1.4, for speech-input users); Ctrl+K always works.
 */
@Injectable({ providedIn: 'root' })
export class ShortcutsService {
  private readonly doc = inject(DOCUMENT);
  private readonly router = inject(Router);
  private readonly registry = inject(CommandRegistry);

  readonly paletteOpen = signal(false);
  readonly helpOpen = signal(false);
  readonly singleKeys = signal(readSingleKeys());

  private sequences: readonly KeySequence[] = [];
  private pending: { prefix: string; at: number } | null = null;

  /** Set by the shell: the page and action sequences it knows about. */
  setSequences(sequences: readonly KeySequence[]): void {
    this.sequences = sequences;
  }

  get allSequences(): readonly KeySequence[] {
    return this.sequences;
  }

  openPalette(): void {
    this.helpOpen.set(false);
    this.paletteOpen.set(true);
  }

  openHelp(): void {
    this.paletteOpen.set(false);
    this.helpOpen.set(true);
  }

  setSingleKeys(on: boolean): void {
    this.singleKeys.set(on);
    try {
      if (on) localStorage.removeItem(SHORTCUTS_STORAGE_KEY);
      else localStorage.setItem(SHORTCUTS_STORAGE_KEY, 'off');
    } catch {
      // Storage blocked: applies for this page view.
    }
  }

  /** Returns true when the key press was a shortcut (and was handled). */
  handle(event: KeyboardEvent, now = Date.now()): boolean {
    if (event.defaultPrevented) return false;
    const key = event.key;

    if ((event.ctrlKey || event.metaKey) && !event.altKey && key.toLowerCase() === 'k') {
      // Leave Ctrl+K alone while another dialog (confirm) is up.
      if (!this.paletteOpen() && this.otherDialogOpen()) return false;
      event.preventDefault();
      if (this.paletteOpen()) this.paletteOpen.set(false);
      else this.openPalette();
      return true;
    }

    if (event.ctrlKey || event.metaKey || event.altKey) return false;
    if (!this.singleKeys() || isTyping(event.target) || this.anyDialogOpen()) {
      this.pending = null;
      return false;
    }

    const pending = this.pending;
    if (pending && now - pending.at < SEQUENCE_WINDOW_MS) {
      this.pending = null;
      const seq = this.sequences.find(
        (s) => s.prefix === pending.prefix && s.key === key.toLowerCase(),
      );
      if (!seq) return false;
      event.preventDefault();
      this.runSequence(seq);
      return true;
    }
    this.pending = null;

    if (key === '?') {
      event.preventDefault();
      this.openHelp();
      return true;
    }
    if (key === '/') {
      event.preventDefault();
      this.openPalette();
      return true;
    }
    if (key === 'g' || key === 'n') {
      this.pending = { prefix: key, at: now };
      return true;
    }
    return false;
  }

  private runSequence(seq: KeySequence): void {
    if (seq.path) {
      void this.router.navigateByUrl(seq.path);
      return;
    }
    const command = this.registry.commands().find((c) => c.id === seq.commandId);
    void command?.run();
  }

  private anyDialogOpen(): boolean {
    return !!this.doc.querySelector('dialog[open]');
  }

  private otherDialogOpen(): boolean {
    return !!this.doc.querySelector('dialog[open]:not([data-palette])');
  }
}

function readSingleKeys(): boolean {
  try {
    return localStorage.getItem(SHORTCUTS_STORAGE_KEY) !== 'off';
  } catch {
    return true;
  }
}

export function isTyping(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  return (
    target.isContentEditable ||
    target instanceof HTMLInputElement ||
    target instanceof HTMLTextAreaElement ||
    target instanceof HTMLSelectElement
  );
}
