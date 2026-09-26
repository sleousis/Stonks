import { HttpTestingController } from '@angular/common/http/testing';
import type { Type } from '@angular/core';
import { type ComponentFixture, TestBed } from '@angular/core/testing';

import { nextRequest, tick } from '../../../testing/http';

/** Test helper for the server-paged orders pages (specs only). */
export interface PagedRun<T> {
  fixture: ComponentFixture<T>;
  /** The offset of each request, in order. */
  offsets: number[];
  /** The pager's range text after the last page loaded ("101–150 of 250"). */
  range: string;
  /** Whether rows stayed on screen (no skeleton) while each next page loaded. */
  keptRowsWhileLoading: boolean[];
}

/**
 * Renders a server-paged page, serves `pages` pages of `size` rows from a
 * total of `size * 5`, and presses Next between them.
 */
export async function pageThrough<T>(
  component: Type<T>,
  path: string,
  size: number,
  row: (i: number) => object,
  pages = 3,
): Promise<PagedRun<T>> {
  const http = TestBed.inject(HttpTestingController);
  const fixture = TestBed.createComponent(component);
  const el = fixture.nativeElement as HTMLElement;
  const offsets: number[] = [];
  const keptRowsWhileLoading: boolean[] = [];
  const settle = async () => {
    for (let i = 0; i < 3; i++) {
      await tick();
      fixture.detectChanges();
    }
  };
  const serve = async () => {
    const req = await nextRequest(http, path);
    const offset = Number(/offset=(\d+)/.exec(req.request.urlWithParams)?.[1] ?? 0);
    offsets.push(offset);
    if (offsets.length > 1) {
      keptRowsWhileLoading.push(
        el.querySelectorAll('tbody tr').length > 0 && !el.querySelector('app-loading-state'),
      );
    }
    const items = Array.from({ length: size }, (_, i) => row(offset + i));
    req.flush({ items, total: size * 5, limit: size, offset });
    await settle();
  };
  const next = () =>
    [...el.querySelectorAll<HTMLButtonElement>('.pager button')]
      .find((b) => b.textContent?.trim() === 'Next')!
      .click();

  fixture.detectChanges();
  await serve();
  for (let p = 1; p < pages; p++) {
    next();
    fixture.detectChanges();
    await serve();
  }
  http.verify();
  const range = el.querySelector('.pager .range')?.textContent?.trim() ?? '';
  return { fixture, offsets, range, keptRowsWhileLoading };
}
