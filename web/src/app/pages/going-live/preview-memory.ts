/**
 * When the owner last ran the dry-run preview of a portfolio, on this
 * browser only. The Going live checklist shows the preview step as done
 * from it. A per-viewer convenience: private windows or blocked storage
 * simply show the step as not done.
 */
const KEY = 'stonks.preview.';

export function rememberPreview(portfolioId: string, at: string = new Date().toISOString()): void {
  try {
    localStorage.setItem(KEY + portfolioId, at);
  } catch {
    // Storage blocked: the checklist shows the step as not done.
  }
}

export function lastPreview(portfolioId: string): string | null {
  try {
    return localStorage.getItem(KEY + portfolioId);
  } catch {
    return null;
  }
}
