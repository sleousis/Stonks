import { TestBed } from '@angular/core/testing';
import { SwUpdate } from '@angular/service-worker';
import { Subject } from 'rxjs';

import { ToastService } from '../notify/toast.service';
import { ConnectivityService } from './connectivity.service';

describe('ConnectivityService', () => {
  it('follows the online and offline events', () => {
    const svc = TestBed.inject(ConnectivityService);
    const onLine = vi.spyOn(navigator, 'onLine', 'get').mockReturnValue(false);
    window.dispatchEvent(new Event('offline'));
    expect(svc.online()).toBe(false);
    onLine.mockReturnValue(true);
    window.dispatchEvent(new Event('online'));
    expect(svc.online()).toBe(true);
    onLine.mockRestore();
  });

  it('says once when a new version is ready', () => {
    const versionUpdates = new Subject<{ type: string }>();
    TestBed.configureTestingModule({
      providers: [{ provide: SwUpdate, useValue: { isEnabled: true, versionUpdates } }],
    });
    TestBed.inject(ConnectivityService);
    const toasts = TestBed.inject(ToastService);
    versionUpdates.next({ type: 'VERSION_DETECTED' });
    expect(toasts.toasts().length).toBe(0);
    versionUpdates.next({ type: 'VERSION_READY' });
    expect(toasts.toasts().map((t) => t.title)).toEqual(['A new version of the console is ready']);
  });
});
