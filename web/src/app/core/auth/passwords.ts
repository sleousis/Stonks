import type { AbstractControl, ValidationErrors, ValidatorFn } from '@angular/forms';

/** Mirrors `stonks.auth.passwords` (MIN_LENGTH, MAX_LENGTH); the server has the last word. */
export const PASSWORD_MIN = 12;
export const PASSWORD_MAX = 256;

/** Letters and digits that cannot be misread (no 0/O, 1/l/I) when read out or typed. */
const ALPHABET = 'abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789';

/**
 * A strong first password for an admin to share: four groups of five from
 * an unambiguous alphabet (over 100 bits), e.g. `h7Kpq-x2MfT-...`.
 */
export function generatePassword(randomByte: () => number = cryptoByte): string {
  const groups: string[] = [];
  for (let g = 0; g < 4; g++) {
    let group = '';
    while (group.length < 5) {
      const byte = randomByte();
      // Rejection sampling keeps every character equally likely.
      if (byte < 256 - (256 % ALPHABET.length)) group += ALPHABET[byte % ALPHABET.length];
    }
    groups.push(group);
  }
  return groups.join('-');
}

function cryptoByte(): number {
  return crypto.getRandomValues(new Uint8Array(1))[0];
}

/** Group validator: `field` must equal `other` (password and its repeat). */
export function sameAs(field: string, other: string): ValidatorFn {
  return (group: AbstractControl): ValidationErrors | null => {
    const a = group.get(field)?.value;
    const b = group.get(other)?.value;
    return a && b && a !== b ? { mismatch: true } : null;
  };
}
