import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';

import { AssistantService } from '../../api/assistant.service';
import type { MeView } from '../../api/models';
import { ModelVersionsService } from '../../api/model-versions.service';
import { OptionsService } from '../../api/options.service';
import { StreamService } from '../../api/stream.service';
import { ADMIN, TRADER } from '../../../testing/auth-fixtures';
import { tick } from '../../../testing/http';
import { SessionService } from '../auth/session.service';
import { FeatureFlagsService } from './feature-flags.service';

describe('FeatureFlagsService', () => {
  const me = signal<MeView | null>(null);
  let status: ReturnType<typeof vi.fn>;
  let underlyings: ReturnType<typeof vi.fn>;
  let stream: ReturnType<typeof vi.fn>;
  let candidates: ReturnType<typeof vi.fn>;

  function create() {
    TestBed.configureTestingModule({
      providers: [
        { provide: SessionService, useValue: { me, isAdmin: () => me()?.role === 'admin' } },
        { provide: AssistantService, useValue: { status } },
        { provide: OptionsService, useValue: { underlyings } },
        { provide: StreamService, useValue: { status: stream } },
        { provide: ModelVersionsService, useValue: { candidates } },
      ],
    });
    return TestBed.inject(FeatureFlagsService);
  }

  beforeEach(() => {
    me.set(null);
    status = vi.fn().mockResolvedValue({ enabled: false });
    underlyings = vi.fn().mockResolvedValue([]);
    stream = vi.fn().mockResolvedValue({ streaming_enabled: false, engines: [] });
    candidates = vi.fn().mockResolvedValue([]);
  });

  it('counts a feature as on until the server says it is off (F42)', async () => {
    const flags = create();
    expect(flags.on('assistant')).toBe(true);
    TestBed.tick();
    expect(status).not.toHaveBeenCalled();
    me.set(TRADER);
    TestBed.tick();
    await tick();
    expect(status).toHaveBeenCalledTimes(1);
    expect(flags.on('assistant')).toBe(false);
    expect(flags.assistantOff()).toBe(true);
  });

  it('keeps the feature when the status cannot be read', async () => {
    status.mockRejectedValue(new Error('offline'));
    const flags = create();
    me.set(TRADER);
    TestBed.tick();
    await tick();
    expect(flags.on('assistant')).toBe(true);
    expect(flags.assistantOff()).toBe(false);
  });

  it('turns options on only when option chains are stored', async () => {
    underlyings.mockResolvedValue([{ underlying: 'SPY.US' }]);
    const flags = create();
    me.set(TRADER);
    TestBed.tick();
    await tick();
    expect(flags.on('options')).toBe(true);
    // A trader never asks for the admin-only engine or model reads.
    expect(stream).not.toHaveBeenCalled();
    expect(candidates).not.toHaveBeenCalled();
  });

  it('hides the engine and model versions from admins while they are off (AO-11)', async () => {
    const flags = create();
    me.set(ADMIN);
    TestBed.tick();
    await tick();
    expect(flags.on('options')).toBe(false);
    expect(flags.on('intraday')).toBe(false);
    expect(flags.on('models')).toBe(false);
  });

  it('shows the engine once it streams and model versions once a fit waits', async () => {
    stream.mockResolvedValue({ streaming_enabled: true, engines: [] });
    candidates.mockResolvedValue([{ strategy_id: 'x', version: 2 }]);
    const flags = create();
    me.set(ADMIN);
    TestBed.tick();
    await tick();
    expect(flags.on('intraday')).toBe(true);
    expect(flags.on('models')).toBe(true);
  });
});
