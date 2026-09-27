import type { AbstractControl, ValidationErrors, ValidatorFn } from '@angular/forms';

/** Mirrors `stonks.auth.passwords` (MIN_LENGTH, MAX_LENGTH); the server has the last word. */
export const PASSWORD_MIN = 12;
export const PASSWORD_MAX = 256;

/** Group validator: `field` must equal `other` (password and its repeat). */
export function sameAs(field: string, other: string): ValidatorFn {
  return (group: AbstractControl): ValidationErrors | null => {
    const a = group.get(field)?.value;
    const b = group.get(other)?.value;
    return a && b && a !== b ? { mismatch: true } : null;
  };
}
