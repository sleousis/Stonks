import { allItems } from './api-call';

describe('allItems', () => {
  it('reads page after page until the total is reached', async () => {
    const pages = [
      { items: [1, 2], total: 3 },
      { items: [3], total: 3 },
    ];
    const offsets: number[] = [];
    const items = await allItems(async (q) => {
      offsets.push(q.offset);
      return pages.shift()!;
    });
    expect(items).toEqual([1, 2, 3]);
    expect(offsets).toEqual([0, 2]);
  });

  it('stops on an empty page even when the total says more', async () => {
    let calls = 0;
    const items = await allItems(async () => {
      calls += 1;
      return { items: [] as number[], total: 5 };
    });
    expect(items).toEqual([]);
    expect(calls).toBe(1);
  });
});
