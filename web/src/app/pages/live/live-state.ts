import type { EngineView, LatencyView, StreamHealthView } from '../../api/models';
import type { PillTone } from '../../shared/ui/status-pill';

/** How an engine stands, in words and a tone, never colour alone. */
export interface EngineState {
  status: string;
  label: string;
  tone: PillTone;
  /** One sentence for the person reading the card. */
  advice: string;
}

export function engineState(e: EngineView): EngineState {
  if (e.deadman === 'stopped') {
    return {
      status: 'stopped',
      label: 'Stopped',
      tone: 'neutral',
      advice: 'The engine was stopped. It makes no decisions until it starts again.',
    };
  }
  if (!e.live) {
    return {
      status: 'unhealthy',
      label: 'Not reporting',
      tone: 'negative',
      advice:
        'The engine has not reported for a while. Its process may have stopped. New entries wait until it reports again.',
    };
  }
  if (e.deadman === 'silent') {
    return {
      status: 'unhealthy',
      label: 'Silent',
      tone: 'negative',
      advice:
        'No bar has closed for a while in market hours. The operator was alerted. Check the price stream.',
    };
  }
  if (e.stream && !e.stream.connected) {
    return {
      status: 'warn',
      label: 'Reconnecting',
      tone: 'warn',
      advice: 'The price stream is reconnecting. New entries wait until prices flow again.',
    };
  }
  if (e.deadman === 'closed') {
    return {
      status: 'ok',
      label: 'Market closed',
      tone: 'neutral',
      advice: 'The market is closed. The engine waits for the next open.',
    };
  }
  return {
    status: 'ok',
    label: 'Running',
    tone: 'positive',
    advice: 'Bars close on time and decisions go out.',
  };
}

const STREAM_WORDS: Record<string, string> = {
  idle: 'Not started',
  connecting: 'Connecting',
  streaming: 'Connected',
  backoff: 'Waiting to reconnect',
  stopped: 'Stopped',
  failed: 'Failed',
};

export function streamWords(s: StreamHealthView): string {
  return STREAM_WORDS[s.state] ?? s.state.replace(/_/g, ' ');
}

/** "12 s", "4 min", "2 h 5 min", or a dash. */
export function secondsText(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return '–';
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s} s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} min`;
  const h = Math.floor(m / 60);
  const rest = m % 60;
  return rest ? `${h} h ${rest} min` : `${h} h`;
}

/** "250 ms" under a second, "1.5 s" above, or a dash. */
export function latencyText(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return '–';
  if (seconds < 1) return `${Math.round(seconds * 1000)} ms`;
  return `${Number(seconds.toFixed(1))} s`;
}

/** "Median under 250 ms, 95% under 1 s", or a note that nothing was measured yet. */
export function latencySummary(l: LatencyView): string {
  if (!l.count) return 'Nothing measured yet';
  return `Median under ${latencyText(l.p50_seconds)}, 95% under ${latencyText(l.p95_seconds)}`;
}

/** Handler errors as "name: n" lines, largest first. */
export function errorLines(errors: Record<string, number>): { name: string; count: number }[] {
  return Object.entries(errors)
    .map(([name, count]) => ({ name, count }))
    .sort((a, b) => b.count - a.count || a.name.localeCompare(b.name));
}
