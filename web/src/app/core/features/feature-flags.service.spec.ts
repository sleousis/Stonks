import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';

import { AssistantService } from '../../api/assistant.service';
import type { MeView } from '../../api/models';
import { TRADER } from '../../../testing/auth-fixtures';
import { tick } from '../../../testing/http';
import { SessionService } from '../auth/session.service';
import { FeatureFlagsService } from './feature-flags.service';

describe('FeatureFlagsService', () => {
  const me = signal<MeView | null>(null);
  let status: ReturnType<typeof vi.fn>;

  function create() {
    TestBed.configureTestingModule({
      providers: [
        { provide: SessionService, useValue: { me } },
        { provide: AssistantService, useValue: { status } },
      ],
    });
    return TestBed.inject(FeatureFlagsService);
  }

  beforeEach(() => {
    me.set(null);
    status = vi.fn().mockResolvedValue({ enabled: false });
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
});
