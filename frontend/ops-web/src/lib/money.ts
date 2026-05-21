// Money is always represented internally as integer micro_cents.
// 1 micro_cent unit = $1e-8, so dollars = micro_cents / 1e8.

const MICRO_CENTS_PER_DOLLAR = 1e8;

const usdFormatter = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

/** Format integer micro_cents as a USD string, e.g. 5000000000 -> "$50.00". */
export function formatUSD(microCents: number): string {
  return usdFormatter.format(microCents / MICRO_CENTS_PER_DOLLAR);
}

/**
 * Parse a user-entered dollar string into integer micro_cents.
 * Multiplies by 1e8 and rounds so we never carry a float internally.
 * Returns NaN if the string isn't a valid number.
 */
export function parseDollarsToMicroCents(dollarText: string): number {
  const cleaned = dollarText.replace(/[^0-9.]/g, '');
  if (cleaned === '' || cleaned === '.') return NaN;
  const dollars = Number(cleaned);
  if (Number.isNaN(dollars)) return NaN;
  return Math.round(dollars * MICRO_CENTS_PER_DOLLAR);
}

/** Format a signed micro_cents diff, e.g. -50000000000 -> "-$500.00". */
export function formatSignedUSD(microCents: number): string {
  const sign = microCents < 0 ? '-' : '+';
  return `${sign}${formatUSD(Math.abs(microCents))}`;
}
