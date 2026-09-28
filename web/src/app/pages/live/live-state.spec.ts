import {
  engineState,
  errorLines,
  latencySummary,
  latencyText,
  secondsText,
  streamWords,
} from './live-state';
import { engine, latency } from './live-test-fixtures';

describe('engine state', () => {
  it('reads running, silent, not reporting, reconnecting, closed and stopped', () => {
    expect(engineState(engine()).label).toBe('Running');
    expect(engineState(engine({ deadman: 'silent' }))).toMatchObject({
      label: 'Silent',
      tone: 'negative',
    });
    expect(engineState(engine({ live: false })).label).toBe('Not reporting');
    const base = engine();
    const dropped = engine({ stream: { ...base.stream!, connected: false, state: 'backoff' } });
    expect(engineState(dropped)).toMatchObject({ label: 'Reconnecting', tone: 'warn' });
    expect(engineState(engine({ deadman: 'closed' })).label).toBe('Market closed');
    expect(engineState(engine({ deadman: 'stopped', live: false })).label).toBe('Stopped');
  });

  it('never says only a colour: each state has advice', () => {
    for (const e of [engine(), engine({ deadman: 'silent' }), engine({ live: false })]) {
      expect(engineState(e).advice.length).toBeGreaterThan(10);
    }
  });
});

describe('live words', () => {
  it('puts stream states in plain words', () => {
    expect(streamWords(engine().stream!)).toBe('Connected');
    expect(streamWords({ ...engine().stream!, state: 'backoff' })).toBe('Waiting to reconnect');
    expect(streamWords({ ...engine().stream!, state: 'odd_state' })).toBe('odd state');
  });

  it('formats seconds and latency', () => {
    expect(secondsText(null)).toBe('–');
    expect(secondsText(12.4)).toBe('12 s');
    expect(secondsText(240)).toBe('4 min');
    expect(secondsText(7500)).toBe('2 h 5 min');
    expect(secondsText(7200)).toBe('2 h');
    expect(latencyText(0.25)).toBe('250 ms');
    expect(latencyText(2.5)).toBe('2.5 s');
    expect(latencyText(undefined)).toBe('–');
  });

  it('summarises a latency histogram', () => {
    expect(latencySummary(latency())).toBe('Nothing measured yet');
    expect(latencySummary(latency({ count: 3, p50_seconds: 0.05, p95_seconds: 1 }))).toBe(
      'Median under 50 ms, 95% under 1 s',
    );
  });

  it('lists failed steps, most failures first', () => {
    expect(errorLines({ b: 1, a: 1, step: 5 })).toEqual([
      { name: 'step', count: 5 },
      { name: 'a', count: 1 },
      { name: 'b', count: 1 },
    ]);
  });
});
