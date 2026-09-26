import {
  EnvironmentInjector,
  Injector,
  createEnvironmentInjector,
  runInInjectionContext,
  signal,
} from '@angular/core';
import { TestBed } from '@angular/core/testing';

import {
  type AutoRefresh,
  type Reloadable,
  RefreshStatus,
  UpdatedAgo,
  autoRefresh,
  updatedLabel,
} from './auto-refresh';

class FakeResource implements Reloadable {
  readonly loading = signal(false);
  readonly loaded = signal(true);
  reloads = 0;
  reload() {
    this.reloads++;
    return true;
  }
  isLoading() {
    return this.loading();
  }
  hasValue() {
    return this.loaded();
  }
}

function setVisibility(state: 'visible' | 'hidden') {
  Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => state });
  document.dispatchEvent(new Event('visibilitychange'));
}

describe('autoRefresh', () => {
  let res: FakeResource;
  let auto: AutoRefresh;
  let injector: Injector;

  beforeEach(() => {
    vi.useFakeTimers();
    setVisibility('visible');
    res = new FakeResource();
    injector = TestBed.inject(Injector);
  });
  afterEach(() => {
    vi.useRealTimers();
    setVisibility('visible');
  });

  function create(options = {}) {
    auto = runInInjectionContext(injector, () => autoRefresh(() => [res], options));
  }

  it('reloads every minute while the tab is visible', () => {
    create();
    vi.advanceTimersByTime(59_000);
    expect(res.reloads).toBe(0);
    vi.advanceTimersByTime(1_000);
    expect(res.reloads).toBe(1);
    vi.advanceTimersByTime(60_000);
    expect(res.reloads).toBe(2);
  });

  it('pauses while the tab is hidden and catches up when it comes back', () => {
    create();
    setVisibility('hidden');
    vi.advanceTimersByTime(5 * 60_000);
    expect(res.reloads).toBe(0);
    setVisibility('visible');
    expect(res.reloads).toBe(1);
  });

  it('refresh() reloads now and restarts the minute', () => {
    create();
    vi.advanceTimersByTime(30_000);
    auto.refresh();
    expect(res.reloads).toBe(1);
    vi.advanceTimersByTime(30_000);
    expect(res.reloads).toBe(1);
    vi.advanceTimersByTime(30_000);
    expect(res.reloads).toBe(2);
  });

  it('stops when its owner is destroyed', () => {
    const owner = createEnvironmentInjector([], TestBed.inject(EnvironmentInjector));
    runInInjectionContext(owner, () => autoRefresh(() => [res]));
    owner.destroy();
    vi.advanceTimersByTime(5 * 60_000);
    expect(res.reloads).toBe(0);
  });

  it('reloads when a trigger changes, not on start', () => {
    const finished = signal(0);
    create({ triggers: [finished] });
    TestBed.tick();
    expect(res.reloads).toBe(0);
    finished.set(1);
    TestBed.tick();
    expect(res.reloads).toBe(1);
  });

  it('records when the resources last finished loading', () => {
    vi.setSystemTime(new Date('2026-09-26T10:00:00Z'));
    create();
    expect(auto.updatedAt()).toBe(Date.parse('2026-09-26T10:00:00Z'));
    res.loading.set(true);
    vi.setSystemTime(new Date('2026-09-26T10:01:00Z'));
    expect(auto.updatedAt()).toBe(Date.parse('2026-09-26T10:00:00Z'));
    res.loading.set(false);
    expect(auto.updatedAt()).toBe(Date.parse('2026-09-26T10:01:00Z'));
  });

  it('reports to a RefreshStatus provided above it', () => {
    const status = new RefreshStatus();
    const scoped = Injector.create({
      parent: injector,
      providers: [{ provide: RefreshStatus, useValue: status }],
    });
    runInInjectionContext(scoped, () => autoRefresh(() => [res]));
    expect(status.updatedAt()).not.toBeNull();
  });
});

describe('updated label', () => {
  it('says how long ago in plain words', () => {
    const now = Date.parse('2026-09-26T10:00:00Z');
    expect(updatedLabel(null, now)).toBe('');
    expect(updatedLabel(now - 20_000, now)).toBe('Updated just now');
    expect(updatedLabel(now - 2 * 60_000, now)).toBe('Updated 2 min ago');
    expect(updatedLabel(now - 3 * 3600_000, now)).toBe('Updated 3 h ago');
  });

  it('renders under the title', async () => {
    const fixture = TestBed.createComponent(UpdatedAgo);
    fixture.componentRef.setInput('at', Date.now() - 2 * 60_000);
    await fixture.whenStable();
    expect((fixture.nativeElement as HTMLElement).textContent).toContain('Updated 2 min ago');
  });
});
