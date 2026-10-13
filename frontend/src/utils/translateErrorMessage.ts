// Backend messages may carry a translation key: "[swap.guard.noMarker] English sentence".
const PREFIX = /^\[([a-zA-Z0-9_.]+)\]\s*(.*)$/s;

export function translateErrorMessage(
  message: string | null | undefined,
  // Any i18next t (TFunction is assignable); kept loose for components that receive t as a prop.
  t: (key: string, options?: Record<string, unknown>) => unknown,
): { text: string; original: string | null } {
  if (!message) return { text: '', original: null };
  const m = PREFIX.exec(message);
  if (!m) return { text: message, original: null };
  const [, key, english] = m;
  const translated = t(key, { defaultValue: '' });
  if (!translated || translated === key || typeof translated !== 'string') return { text: english, original: null };
  return { text: translated, original: english };
}
