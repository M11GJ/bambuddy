import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../api/client';
import type { AppSettings } from '../api/client';
import { Card, CardContent, CardHeader } from './Card';
import { Button } from './Button';
import { useToast } from '../contexts/ToastContext';

const INPUT = 'w-full bg-gray-800 border border-gray-700 rounded px-3 py-2 text-white text-sm';

/** Plate-check AI (SwapMod): global Ollama vision model settings, saved with the Save button. */
export function SwapAiSettings() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { showToast } = useToast();
  const { data: settings } = useQuery({ queryKey: ['settings'], queryFn: api.getSettings });
  const [mode, setMode] = useState<AppSettings['swap_ai_mode']>('shadow');
  const [url, setUrl] = useState('');
  const [model, setModel] = useState('');
  const [timeout, setTimeoutS] = useState('60');
  const [maxWait, setMaxWait] = useState('300');

  useEffect(() => {
    if (!settings) return;
    setMode(settings.swap_ai_mode ?? 'shadow');
    setUrl(settings.swap_ai_url ?? '');
    setModel(settings.swap_ai_model ?? '');
    setTimeoutS(String(settings.swap_ai_timeout_s ?? 60));
    setMaxWait(String(settings.swap_ai_max_wait_s ?? 300));
  }, [settings]);

  const save = useMutation({
    mutationFn: () =>
      api.updateSettings({
        swap_ai_mode: mode,
        swap_ai_url: url.trim(),
        swap_ai_model: model.trim(),
        swap_ai_timeout_s: Number(timeout),
        swap_ai_max_wait_s: Number(maxWait),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['settings'] });
      showToast(t('settings.toast.settingsSaved'));
    },
    onError: (e: Error) => showToast(e.message, 'error'),
  });

  return (
    <Card className="mt-4">
      <CardHeader>
        <h2 className="text-lg font-semibold text-white">{t('swap.ai.title')}</h2>
      </CardHeader>
      <CardContent className="space-y-4">
        <p className="text-sm text-bambu-gray">{t('swap.ai.description')}</p>
        <div>
          <label htmlFor="swap-ai-mode" className="block text-sm text-bambu-gray mb-1">{t('swap.ai.mode')}</label>
          <select
            id="swap-ai-mode"
            value={mode}
            onChange={(e) => setMode(e.target.value as AppSettings['swap_ai_mode'])}
            className={INPUT}
          >
            <option value="off">{t('swap.ai.modeOff')}</option>
            <option value="shadow">{t('swap.ai.modeShadow')}</option>
            <option value="enforce">{t('swap.ai.modeEnforce')}</option>
          </select>
        </div>
        <div>
          <label htmlFor="swap-ai-url" className="block text-sm text-bambu-gray mb-1">{t('swap.ai.url')}</label>
          <input id="swap-ai-url" type="text" value={url} onChange={(e) => setUrl(e.target.value)} className={INPUT} />
          <p className="text-xs text-bambu-gray mt-1">{t('swap.ai.urlHint')}</p>
        </div>
        <div>
          <label htmlFor="swap-ai-model" className="block text-sm text-bambu-gray mb-1">{t('swap.ai.model')}</label>
          <input id="swap-ai-model" type="text" value={model} onChange={(e) => setModel(e.target.value)} className={INPUT} />
        </div>
        <div className="grid grid-cols-2 gap-3">
          <div>
            <label htmlFor="swap-ai-timeout" className="block text-sm text-bambu-gray mb-1">{t('swap.ai.timeout')}</label>
            <input id="swap-ai-timeout" type="number" min={1} max={600} value={timeout} onChange={(e) => setTimeoutS(e.target.value)} className={INPUT} />
          </div>
          <div>
            <label htmlFor="swap-ai-maxwait" className="block text-sm text-bambu-gray mb-1">{t('swap.ai.maxWait')}</label>
            <input id="swap-ai-maxwait" type="number" min={1} max={3600} value={maxWait} onChange={(e) => setMaxWait(e.target.value)} className={INPUT} />
          </div>
        </div>
        <Button onClick={() => save.mutate()} disabled={save.isPending}>{t('common.save')}</Button>
      </CardContent>
    </Card>
  );
}
