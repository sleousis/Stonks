import { activeFormat, browserFormat } from '../../core/format/format';
import { daySpine } from './day-spine';

const MARKET = {
  calendar: 'XNYS',
  is_open: false,
  today: {
    date: '2026-09-28',
    pre_open: '2026-09-28T08:00:00Z',
    open: '2026-09-28T13:30:00Z',
    close: '2026-09-28T20:00:00Z',
  },
  next: {
    date: '2026-09-29',
    pre_open: '2026-09-29T08:00:00Z',
    open: '2026-09-29T13:30:00Z',
    close: '2026-09-29T20:00:00Z',
  },
};

describe('daySpine', () => {
  beforeEach(() => activeFormat.set({ locale: 'en-US', timeZone: 'UTC', dateStyle: 'iso' }));
  afterEach(() => activeFormat.set(browserFormat()));

  it('lays the session over the local day: pre hatched, regular, then after hours', () => {
    const s = daySpine(MARKET, Date.parse('2026-09-28T06:00:00Z'));
    expect(s.segments.map((x) => x.kind)).toEqual(['pre', 'regular', 'post']);
    expect(s.segments[1].left).toBeCloseTo(13.5 / 24);
    expect(s.segments[1].width).toBeCloseTo(6.5 / 24);
    expect(s.now).toBeCloseTo(6 / 24);
  });

  it('splits a stretch that runs past midnight', () => {
    const late = {
      ...MARKET,
      today: { ...MARKET.today, close: '2026-09-28T22:00:00Z' },
    };
    const post = daySpine(late, Date.parse('2026-09-28T06:00:00Z')).segments.filter(
      (x) => x.kind === 'post',
    );
    expect(post).toHaveLength(2);
    expect(post[1].left).toBe(0);
    expect(post[1].width).toBeCloseTo(2 / 24);
  });

  it('marks the next run within a day and drops a label that would overlap', () => {
    const s = daySpine(MARKET, Date.parse('2026-09-28T06:00:00Z'), '2026-09-28T20:45:00Z');
    const run = s.marks.find((m) => m.kind === 'run')!;
    expect(run.label).toBe('Run 20:45');
    expect(run.labelled).toBe(true);
    expect(s.marks.find((m) => m.kind === 'close')!.labelled).toBe(false);
    expect(s.marks.find((m) => m.kind === 'open')!.labelled).toBe(true);
    const far = daySpine(MARKET, Date.parse('2026-09-28T06:00:00Z'), '2026-10-02T20:45:00Z');
    expect(far.marks.some((m) => m.kind === 'run')).toBe(false);
  });
});
