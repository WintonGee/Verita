// Money + date formatting helpers.
//
// All *_micro_cents are integers where 1 unit = $1e-8.
// dollars = micro_cents / 1e8. We only ever convert to float for *display*;
// never do money math (sums, diffs) in floats.

const usdFormatter = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
});

/**
 * Format an integer micro_cents value as a USD string for display only.
 * @param microCents integer micro-cents (1e-8 USD per unit)
 * @param currency ISO currency code (default USD); falls back to USD formatter
 */
export function formatMoney(microCents: number, currency = 'USD'): string {
  const dollars = microCents / 1e8;
  if (currency === 'USD') return usdFormatter.format(dollars);
  return new Intl.NumberFormat('en-US', { style: 'currency', currency }).format(
    dollars,
  );
}

/**
 * Format a per-unit price, which can be sub-cent (e.g. $0.001/unit). Shows up
 * to 8 decimals with trailing zeros trimmed, so a $0.001 rate doesn't collapse
 * to "$0.00" the way the 2-decimal currency formatter would.
 */
export function formatUnitPrice(microCents: number): string {
  const dollars = microCents / 1e8;
  if (dollars === 0) return '$0';
  return '$' + dollars.toFixed(8).replace(/\.?0+$/, '');
}

const numberFormatter = new Intl.NumberFormat('en-US');

export function formatUnits(units: number | null | undefined): string {
  if (units == null) return '—';
  return numberFormatter.format(units);
}

/** Format an ISO date as a short calendar date (UTC). */
export function formatDate(iso: string | null | undefined): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '—';
  return d.toLocaleDateString('en-US', {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    timeZone: 'UTC',
  });
}

/** Compact day label for chart x-axis, e.g. "May 3". */
export function formatDayLabel(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString('en-US', {
    month: 'short',
    day: 'numeric',
    timeZone: 'UTC',
  });
}

/** ISO string for the first instant of the current calendar month, in UTC. */
export function currentMonthStartISO(now = new Date()): string {
  return new Date(
    Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), 1, 0, 0, 0, 0),
  ).toISOString();
}
