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
