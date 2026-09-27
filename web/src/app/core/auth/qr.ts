import { encode } from 'uqr';

/**
 * QR codes for the authenticator set-up, drawn in the browser so the
 * secret never goes to another service. `uqr` stays behind this file.
 */

/** Dark (true) and light modules, row by row, no quiet zone. */
export function qrModules(text: string): boolean[][] {
  return encode(text, { ecc: 'M', border: 0 }).data;
}

/** One SVG path with a unit square per dark module (viewBox `0 0 size size`). */
export function qrPath(modules: readonly (readonly boolean[])[]): string {
  let d = '';
  modules.forEach((row, y) =>
    row.forEach((dark, x) => {
      if (dark) d += `M${x} ${y}h1v1h-1z`;
    }),
  );
  return d;
}
