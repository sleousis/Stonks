import { mapSettledLimit } from './map-limit';

describe('mapSettledLimit', () => {
  it('keeps at most the limit in flight and returns results in order', async () => {
    let inFlight = 0;
    let peak = 0;
    const results = await mapSettledLimit([1, 2, 3, 4, 5, 6, 7], 3, async (n) => {
      inFlight++;
      peak = Math.max(peak, inFlight);
      await new Promise((r) => setTimeout(r, 1));
      inFlight--;
      if (n === 4) throw new Error('no');
      return n * 10;
    });
    expect(peak).toBe(3);
    expect(results.map((r) => (r.status === 'fulfilled' ? r.value : 'x'))).toEqual([
      10,
      20,
      30,
      'x',
      50,
      60,
      70,
    ]);
  });

  it('settles an empty list at once', async () => {
    expect(await mapSettledLimit([], 4, async () => 1)).toEqual([]);
  });
});
