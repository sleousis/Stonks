import { TestBed } from '@angular/core/testing';

import { ScreenerService } from '../../api/screener.service';
import { SaveUniverseSheet } from './save-universe-sheet';

describe('SaveUniverseSheet', () => {
  it('cannot be cancelled while the save is on its way, and reports the saved universe', async () => {
    let answer!: (v: unknown) => void;
    const saveAsUniverse = vi.fn(() => new Promise((resolve) => (answer = resolve)));
    TestBed.configureTestingModule({
      providers: [{ provide: ScreenerService, useValue: { saveAsUniverse } }],
    });
    const fixture = TestBed.createComponent(SaveUniverseSheet);
    fixture.detectChanges();
    const sheet = fixture.componentInstance;
    const result = sheet.open({ filters: [] } as never, 'scr_1', 'Cheap payers');
    fixture.detectChanges();
    const el = fixture.nativeElement as HTMLElement;
    el.querySelector<HTMLButtonElement>('button[type="submit"]')!.click();
    fixture.detectChanges();
    expect(saveAsUniverse).toHaveBeenCalledTimes(1);
    const cancel = [...el.querySelectorAll<HTMLButtonElement>('button')].find(
      (b) => b.textContent?.trim() === 'Cancel',
    )!;
    expect(cancel.disabled).toBe(true);
    (sheet as unknown as { close(r: null): void }).close(null);
    const saved = { universe_id: 'cheap-payers' };
    answer(saved);
    expect(await result).toEqual(saved);
  });
});
