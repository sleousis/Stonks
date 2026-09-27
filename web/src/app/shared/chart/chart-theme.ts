import type { ChartTheme } from './chart-engine';

/** The chart colours and type, read from the design tokens of the current theme. */
export function readChartTheme(doc: Document): ChartTheme {
  const css = doc.defaultView?.getComputedStyle(doc.documentElement);
  const v = (name: string, fallback: string) => css?.getPropertyValue(name).trim() || fallback;
  return {
    background: v('--color-surface', '#ffffff'),
    text: v('--color-ink-3', '#5f6b78'),
    grid: v('--chart-grid', '#e3e8ed'),
    border: v('--color-border', '#d3dae1'),
    font: v('--font-sans', 'system-ui'),
    colors: {
      brass: v('--color-brass', '#a26d12'),
      primary: v('--color-primary', '#22477a'),
      gain: v('--color-gain', '#17784a'),
      loss: v('--color-loss', '#b8342a'),
      muted: v('--color-ink-3', '#5f6b78'),
      info: v('--color-info', '#2a5db0'),
      warn: v('--color-warn', '#8f5d00'),
      violet: v('--chart-violet', '#6a3fa0'),
      ink: v('--color-ink-2', '#45515e'),
    },
  };
}
