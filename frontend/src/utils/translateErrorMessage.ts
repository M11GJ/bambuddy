// Backend messages may carry a translation key: "[swap.guard.noMarker] English sentence",
// possibly after leading text ("X1C: [swap.hold.outOfPlates] ..."). Only the first key is
// translated; leading text is kept. Keys are dotted and start with a letter,
// so "[3]" or "[0.4]" is not one; an unknown key after leading text leaves the message as is
// (it may be a bracketed file name).
const KEY = /\[([a-zA-Z][a-zA-Z0-9_]*(?:\.[a-zA-Z0-9_]+)+)\]\s*/;

export function translateErrorMessage(
  message: string | null | undefined,
  // Any i18next t (TFunction is assignable); kept loose for components that receive t as a prop.
  t: (key: string, options?: Record<string, unknown>) => unknown,
): { text: string; original: string | null } {
  if (!message) return { text: '', original: null };
  const m = KEY.exec(message);
  if (!m) return { text: message, original: null };
  const key = m[1];
  const lead = message.slice(0, m.index);
  const english = message.slice(m.index + m[0].length);
  const translated = t(key, { defaultValue: '' });
  if (!translated || translated === key || typeof translated !== 'string') {
    return { text: lead ? message : english, original: null };
  }
  return { text: lead + translated, original: english };
}
