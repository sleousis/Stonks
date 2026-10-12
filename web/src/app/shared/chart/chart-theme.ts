import type { ChartTheme } from './chart-engine';

/**
 * The chart colours and type, read from the design tokens of the current
 * theme. Axis and crosshair labels are figures (prices, dates), so they are
 * set in the mono face like every other figure.
 */
export function readChartTheme(doc: Document): ChartTheme {
  const css = doc.defaultView?.getComputedStyle(doc.documentElement);
  const v = (name: string, fallback: string) => css?.getPropertyValue(name).trim() || fallback;
  return {
    background: v('--bg', '#f6f7f9'),
    text: v('--chart-text', '#5a6672'),
    grid: v('--chart-grid', '#e3e8ed'),
    border: v('--rule', '#dfe2e8'),
    font: v('--font-mono', 'ui-monospace, monospace'),
    colors: {
      brass: v('--brass', '#a26d12'),
      primary: v('--accent', '#22477a'),
      gain: v('--up', '#17784a'),
      loss: v('--down', '#b8342a'),
      muted: v('--muted', '#5f6b78'),
      info: v('--accent', '#2a5db0'),
      warn: v('--warn', '#8f5d00'),
      violet: v('--chart-violet', '#6a3fa0'),
      ink: v('--ink-2', '#45515e'),
    },
  };
}
