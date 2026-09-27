import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { NO_ERRORS_SCHEMA } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import type { TickRun } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { nextRequest, tick } from '../../../testing/http';
import { pageThrough } from './server-paging.testing';
import { TickRunner } from './tick-runner';
import { TicksPage } from './ticks.page';

function run(i: number): TickRun {
  return {
    id: `t${i}`,
    status: 'ok',
    started_at: '2026-09-25T20:45:00Z',
    finished_at: '2026-09-25T20:46:00Z',
    summary: null,
  };
}

describe('TicksPage', () => {
  let controller: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [...provideApi(), provideHttpClientTesting(), provideRouter([])],
    });
    // The runner has its own spec; here it would add broker and session calls.
    TestBed.overrideComponent(TicksPage, {
      remove: { imports: [TickRunner] },
      add: { schemas: [NO_ERRORS_SCHEMA] },
    });
    controller = TestBed.inject(HttpTestingController);
  });

  it('Next twice loads offset 25 then 50 and the range reads 51–75', async () => {
    const result = await pageThrough(TicksPage, '/api/ticks', 25, run);
    expect(result.offsets).toEqual([0, 25, 50]);
    expect(result.range).toBe('51–75 of 125');
    expect(result.keptRowsWhileLoading).toEqual([true, true]);
  });

  it('Next four times reaches offset 100 and the range reads 101–125', async () => {
    const result = await pageThrough(TicksPage, '/api/ticks', 25, run, 5);
    expect(result.offsets).toEqual([0, 25, 50, 75, 100]);
    expect(result.range).toBe('101–125 of 125');
  });

  it('calls them trading runs and links each to its detail by time', async () => {
    const fixture = TestBed.createComponent(TicksPage);
    fixture.detectChanges();
    (await nextRequest(controller, '/api/ticks')).flush({
      items: [run(1)],
      total: 1,
      limit: 25,
      offset: 0,
    });
    for (let i = 0; i < 3; i++) {
      await tick();
      fixture.detectChanges();
    }
    const el = fixture.nativeElement as HTMLElement;
    expect(el.querySelector('#ticks-title')?.textContent).toBe('Trading runs');
    const link = el.querySelector<HTMLAnchorElement>('a[href="/orders/ticks/t1"]');
    expect(link?.textContent).not.toContain('t1');
  });

  it('suggests a dry run when nothing has run', async () => {
    const fixture = TestBed.createComponent(TicksPage);
    fixture.detectChanges();
    (await nextRequest(controller, '/api/ticks')).flush({
      items: [],
      total: 0,
      limit: 25,
      offset: 0,
    });
    for (let i = 0; i < 3; i++) {
      await tick();
      fixture.detectChanges();
    }
    const text = (fixture.nativeElement as HTMLElement).textContent ?? '';
    expect(text).toContain('No trading runs yet');
    expect(text).toContain('dry run');
  });
});
