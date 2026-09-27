import { Injectable, computed, signal } from '@angular/core';

const TOKEN_KEY = 'stonks.apiToken';

/**
 * Holds a personal API token the trader pasted (sign-in page or Settings).
 * The auth interceptor sends it on every API request, reads included.
 *
 * The token lives in memory and in sessionStorage only, so it is gone when the
 * tab closes. Never log it, put it in a URL, or copy it to localStorage.
 */
@Injectable({ providedIn: 'root' })
export class AuthTokenService {
  private readonly tokenSignal = signal<string | null>(read(TOKEN_KEY));

  readonly token = this.tokenSignal.asReadonly();
  readonly hasToken = computed(() => !!this.tokenSignal());

  setToken(token: string | null): void {
    const value = token?.trim() || null;
    this.tokenSignal.set(value);
    write(TOKEN_KEY, value);
  }

  clear(): void {
    this.setToken(null);
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
