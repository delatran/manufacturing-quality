import { createContext, useContext, useEffect, useMemo, useState } from 'react';
import type { ReactNode } from 'react';
import { applicationUrl } from './app-url';

export type Locale = 'en' | 'vi';
const storageKey = 'analyst.workspace.locale.v1';
const locales: Record<Locale, string> = { en: 'en-US', vi: 'vi-VN' };

import catalog from './ui-messages.json';
import glossary from '../../knowledge/glossary.json';

const vietnameseLabels: Record<string, string> = catalog.sourceTranslations;
const messages: Record<string, { en: string; vi: string }> = catalog.messages;
export type Translate = (
  key: string,
  valuesOrVietnamese?: Record<string, unknown> | string,
) => string;

const numberFormatters = new Map<string, Intl.NumberFormat>();
function getNumberFormatter(localeCode: string, decimals: number): Intl.NumberFormat {
  const key = `${localeCode}_${decimals}`;
  let formatter = numberFormatters.get(key);
  if (!formatter) {
    formatter = new Intl.NumberFormat(localeCode, {
      minimumFractionDigits: decimals,
      maximumFractionDigits: decimals,
    });
    numberFormatters.set(key, formatter);
  }
  return formatter;
}

const enMonthFmt = new Intl.DateTimeFormat('en-US', {
  month: 'short',
  year: 'numeric',
  timeZone: 'UTC',
});
const viDateFmt = new Intl.DateTimeFormat('vi-VN', {
  day: '2-digit',
  month: '2-digit',
  year: 'numeric',
  timeZone: 'UTC',
});
const enDateFmt = new Intl.DateTimeFormat('en-US', {
  day: 'numeric',
  month: 'short',
  year: 'numeric',
  timeZone: 'UTC',
});

const glossaryEnToVi = new Map<string, string>(
  Object.values(glossary.terms).map((value) => [value.en, value.vi]),
);

const scenarioKeyMap: Record<string, string> = {
  exclude_cost_10: 'ui.scenario_exclude_cost_10',
  ambiguous_dm: 'ui.scenario_ambiguous_dm',
  year_2024: 'ui.scenario_year_2024',
};

const linePattern = /^Line-\d+$/;
const categoryPattern = /^Category \d+$/;
const productPattern = /^Product [A-Z0-9]+$/;
const yyyyMmPattern = /^\d{4}-\d{2}$/;
const yyyyMmDdPattern = /^\d{4}-\d{2}-\d{2}$/;

const failurePatterns: [RegExp, string][] = [
  [/^Request failed \((\d+)\)\.$/, 'Yêu cầu thất bại (mã $1).'],
  [/^HTTP source request failed: (.+)$/, 'Không tải được nguồn HTTP: $1'],
  [/^HTTP source returned status (\d+)$/, 'Nguồn HTTP trả về mã $1'],
  [
    /^The HTTP source is unavailable and no complete snapshot is loaded: (.+)$/,
    'Nguồn HTTP không khả dụng và chưa tải được bản dữ liệu đầy đủ: $1',
  ],
];

export function createI18n(locale: Locale, setLocale: (value: Locale) => void = () => {}) {
  const t: Translate = (key, valuesOrVietnamese) => {
    const message = messages[key];
    let result =
      message?.[locale] ??
      (locale === 'vi'
        ? typeof valuesOrVietnamese === 'string'
          ? valuesOrVietnamese
          : vietnameseLabels[key] || key
        : key);
    if (valuesOrVietnamese && typeof valuesOrVietnamese === 'object')
      result = result.replace(/\{(\w+)\}/g, (match, name: string) =>
        String(valuesOrVietnamese[name] ?? match),
      );
    return result;
  };
  const term = (key: keyof typeof glossary.terms) => glossary.terms[key][locale];
  const number = (value: number | null | undefined, decimals = 0) =>
    value == null || !Number.isFinite(value)
      ? t('ui.unavailable')
      : getNumberFormatter(locales[locale], decimals).format(value);
  const percent = (value: number | null | undefined) =>
    value == null || !Number.isFinite(value) ? number(value) : `${number(value, 1)}%`;
  const month = (value: string) => {
    if (!yyyyMmPattern.test(value)) return value;
    if (locale === 'vi') return `Tháng ${Number(value.slice(5))}/${value.slice(0, 4)}`;
    return enMonthFmt.format(new Date(`${value}-01T00:00:00Z`));
  };
  const date = (value: string) => {
    if (yyyyMmDdPattern.test(value)) {
      const day = new Date(`${value}T00:00:00Z`);
      return Number.isNaN(day.getTime())
        ? value
        : (locale === 'vi' ? viDateFmt : enDateFmt).format(day);
    }
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString(locales[locale]);
  };
  const label = (raw: string) => {
    const mappedScenario = scenarioKeyMap[raw];
    if (mappedScenario) return t(mappedScenario);
    if (locale !== 'vi') return raw;
    if (vietnameseLabels[raw]) return vietnameseLabels[raw];
    const glossaryVi = glossaryEnToVi.get(raw);
    if (glossaryVi) return glossaryVi;
    if (linePattern.test(raw)) return raw.replace('Line-', 'Chuyền ');
    if (categoryPattern.test(raw)) return raw.replace('Category ', 'Nhóm ');
    if (productPattern.test(raw)) return raw.replace('Product ', 'Sản phẩm ');
    for (const [pattern, translated] of failurePatterns) {
      if (pattern.test(raw)) return raw.replace(pattern, translated);
    }
    return raw;
  };
  const download = (url: string) =>
    `${applicationUrl(url)}${url.includes('?') ? '&' : '?'}locale=${locale}`;
  const scope = (filters: Record<string, unknown>) => {
    const period =
      filters.start_month && filters.end_month
        ? `${month(String(filters.start_month))} ${t('ui.to')} ${month(String(filters.end_month))}`
        : '';
    const selected = [
      'production_line',
      'category',
      'product_id',
      'severity',
      'defect_type',
      'defect_location',
      'inspection_method',
      'scenario',
    ]
      .filter((key) => filters[key])
      .map((key) =>
        key === 'product_id'
          ? `${t('ui.filter_product')} #${String(filters[key])}`
          : label(String(filters[key])),
      );
    return [period, ...selected].filter(Boolean).join(' · ') || t('ui.all_available_data');
  };
  return { locale, setLocale, t, term, number, percent, month, date, label, download, scope };
}

const I18nContext = createContext(createI18n('en'));

export function I18nProvider({
  children,
  initialLocale,
  persist = true,
}: {
  children: ReactNode;
  initialLocale?: Locale;
  persist?: boolean;
}) {
  const [locale, setLocale] = useState<Locale>(() => {
    if (initialLocale) return initialLocale;
    try {
      const saved = window.localStorage.getItem(storageKey);
      if (saved === 'vi' || saved === 'en') return saved;
      return typeof navigator !== 'undefined' && navigator.language.toLowerCase().startsWith('vi')
        ? 'vi'
        : 'en';
    } catch {
      return typeof navigator !== 'undefined' && navigator.language.toLowerCase().startsWith('vi')
        ? 'vi'
        : 'en';
    }
  });
  useEffect(() => {
    document.documentElement.lang = locale;
    document.title = createI18n(locale).t('ui.workspace_title');
    if (persist) {
      try {
        window.localStorage.setItem(storageKey, locale);
      } catch {
        /* Optional preference. */
      }
    }
  }, [locale, persist]);
  const value = useMemo(() => createI18n(locale, setLocale), [locale]);
  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

export const useI18n = () => useContext(I18nContext);
