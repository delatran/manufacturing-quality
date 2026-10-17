const enShortMonthFmt = new Intl.DateTimeFormat('en-US', {
  month: 'short',
  timeZone: 'UTC',
});

export function shortMonth(value: string, locale: string) {
  return locale === 'vi'
    ? `T${Number(value.slice(5))}`
    : enShortMonthFmt.format(new Date(`${value}-01T00:00:00Z`));
}
