import { Injectable, computed, signal } from '@angular/core';

const TOKEN_KEY = 'stonks.apiToken';
const READS_KEY = 'stonks.sendTokenOnReads';

/**
 * Holds the API bearer token (STONKS_API_TOKEN) the trader enters in Settings.
 *
 * The token lives in memory and in sessionStorage only, so it is gone when the
 * tab closes. Never log it, put it in a URL, or copy it to localStorage.
 */
@Injectable({ providedIn: 'root' })
export class AuthTokenService {
  private readonly tokenSignal = signal<string | null>(read(TOKEN_KEY));
  private readonly sendOnReadsSignal = signal<boolean>(read(READS_KEY) === '1');

  readonly token = this.tokenSignal.asReadonly();
  readonly hasToken = computed(() => !!this.tokenSignal());
  /** Also send the token on GET requests (needed when reads are not open, e.g. remote hosts). */
  readonly sendOnReads = this.sendOnReadsSignal.asReadonly();

  setToken(token: string | null): void {
    const value = token?.trim() || null;
    this.tokenSignal.set(value);
    write(TOKEN_KEY, value);
  }

  clear(): void {
    this.setToken(null);
  }

  setSendOnReads(on: boolean): void {
    this.sendOnReadsSignal.set(on);
    write(READS_KEY, on ? '1' : null);
  }
}

function read(key: string): string | null {
  try {
    return sessionStorage.getItem(key);
  } catch {
    return null;
  }
}

function write(key: string, value: string | null): void {
  try {
    if (value === null) sessionStorage.removeItem(key);
    else sessionStorage.setItem(key, value);
  } catch {
    // Storage blocked (private mode, policy): the in-memory value still works.
  }
}
