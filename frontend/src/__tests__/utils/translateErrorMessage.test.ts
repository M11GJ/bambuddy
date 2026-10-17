import { describe, expect, it } from 'vitest';
import { translateErrorMessage } from '../../utils/translateErrorMessage';

const t = ((key: string) => (key === 'swap.guard.noMarker' ? 'SwapMod の交換動作がありません' : key)) as never;

describe('translateErrorMessage', () => {
  it('translates a [key] prefix and keeps the English original', () => {
    expect(translateErrorMessage('[swap.guard.noMarker] The print file has no SwapMod swap block.', t)).toEqual({
      text: 'SwapMod の交換動作がありません',
      original: 'The print file has no SwapMod swap block.',
    });
  });
  it('passes plain messages through', () => {
    expect(translateErrorMessage('Printer offline', t)).toEqual({ text: 'Printer offline', original: null });
  });
  it('falls back to the original when the key is unknown', () => {
    expect(translateErrorMessage('[swap.unknown] Something', t)).toEqual({ text: 'Something', original: null });
  });
  it('handles empty input', () => {
    expect(translateErrorMessage(null, t)).toEqual({ text: '', original: null });
  });
});

describe('translateErrorMessage mid-string keys', () => {
  it('translates the first key after leading text and keeps the lead', () => {
    expect(translateErrorMessage('X1C: [swap.guard.noMarker] Swap queue stopped: X1C', t)).toEqual({
      text: 'X1C: SwapMod の交換動作がありません',
      original: 'Swap queue stopped: X1C',
    });
  });
  it('leaves a message with an unknown mid-string key untouched', () => {
    expect(translateErrorMessage('Could not open [part.v2.3mf] file', t)).toEqual({
      text: 'Could not open [part.v2.3mf] file',
      original: null,
    });
  });
  it('does not treat numeric brackets as keys', () => {
    expect(translateErrorMessage('[0.4] nozzle mismatch', t)).toEqual({ text: '[0.4] nozzle mismatch', original: null });
  });
});

describe('translateErrorMessage swap.gate.aiPending (real en locale)', () => {
  it('translates the waiting reason', async () => {
    const en = (await import('../../i18n/locales/en')).default as { swap: { gate: { aiPending: string } } };
    const real = ((key: string) => (key === 'swap.gate.aiPending' ? en.swap.gate.aiPending : key)) as never;
    expect(translateErrorMessage('[swap.gate.aiPending] Waiting for the plate check AI (12 s)', real)).toEqual({
      text: 'Waiting for the plate check AI',
      original: 'Waiting for the plate check AI (12 s)',
    });
  });
});
