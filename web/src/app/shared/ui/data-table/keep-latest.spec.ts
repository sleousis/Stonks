import { resource, signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';

import { tick } from '../../../../testing/http';
import { keepLatest } from './keep-latest';

describe('keepLatest', () => {
  it('keeps the previous value while new params load, and drops it on error', async () => {
    const page = signal(0);
    const calls: { resolve: (v: string) => void; reject: (e: unknown) => void }[] = [];
    const { res, shown } = TestBed.runInInjectionContext(() => {
      const res = resource({
        params: () => ({ page: page() }),
        loader: () =>
          new Promise<string>((resolve, reject) => {
            calls.push({ resolve, reject });
          }),
      });
      return { res, shown: keepLatest(res) };
    });
    const settle = async () => {
      TestBed.tick();
      await tick();
    };

    await settle();
    expect(shown()).toBeUndefined();
    calls.shift()!.resolve('page 0');
    await settle();
    expect(shown()).toBe('page 0');

    page.set(1);
    await settle();
    expect(res.hasValue()).toBe(false);
    expect(shown()).toBe('page 0');
    calls.shift()!.resolve('page 1');
    await settle();
    expect(shown()).toBe('page 1');

    page.set(2);
    await settle();
    calls.shift()!.reject(new Error('boom'));
    await settle();
    expect(shown()).toBeUndefined();
  });
});
