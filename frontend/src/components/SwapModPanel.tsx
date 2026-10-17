import { useEffect, useState } from 'react';
import { parseUTCDate } from '../utils/date';
import { useTranslation } from 'react-i18next';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Loader2, Trash2 } from 'lucide-react';
import { api } from '../api/client';
import type { PrinterStatus, SwapConfig, SwapRecord, SwapLiveState, SwapStatus } from '../api/client';
import { useAuth } from '../contexts/AuthContext';
import { useToast } from '../contexts/ToastContext';
import { translateErrorMessage } from '../utils/translateErrorMessage';

const BUTTON =
  'inline-flex items-center justify-center gap-2 px-3 py-1.5 rounded-lg bg-yellow-100 dark:bg-yellow-500/20 border border-yellow-300 dark:border-yellow-400/40 text-yellow-700 dark:text-yellow-400 hover:bg-yellow-500/30 transition-colors text-xs font-medium disabled:opacity-50';
const INPUT_BASE =
  'px-2 py-1 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white text-sm focus:border-bambu-green focus:outline-none';
const INPUT = `w-full ${INPUT_BASE}`;

function validPlateCount(value: string): number | null {
  if (!/^\d{1,2}$/.test(value.trim())) return null;
  return Number(value.trim());
}

/** Printer card block for a build-plate swapper: spare plates, why the queue is held, plates / resume. */
export function SwapModPanel({ printerId, swap }: { printerId: number; swap?: SwapLiveState | null }) {
  const { t } = useTranslation();
  const { hasPermission } = useAuth();
  const { showToast } = useToast();
  const queryClient = useQueryClient();
  const [plates, setPlates] = useState('');

  // The response is the fresh DB state; write it into the card's status so the
  // panel updates before the next WebSocket push.
  const apply = (s: SwapStatus) => {
    queryClient.setQueryData(['printerStatus', printerId], (old: PrinterStatus | undefined) =>
      old ? { ...old, swap: { enabled: s.enabled, plates_remaining: s.plates_remaining, hold_reason: s.hold_reason } } : old,
    );
    queryClient.invalidateQueries({ queryKey: ['swap', printerId] });
  };
  const onError = (error: Error) =>
    showToast(translateErrorMessage(error.message, t).text || t('printers.toast.failedToSendCommand'), 'error');

  const platesMutation = useMutation({
    mutationFn: (remaining: number) => api.setSwapPlates(printerId, remaining),
    onSuccess: (s) => {
      apply(s);
      setPlates('');
    },
    onError,
  });
  const poseMutation = useMutation({
    mutationFn: () => api.restoreSwapPose(printerId),
    onSuccess: () => showToast(t('swap.poseSent'), 'success'),
    onError,
  });

  const resumeMutation = useMutation({
    mutationFn: () => api.resumeSwap(printerId),
    onSuccess: apply,
    onError,
  });

  if (!swap?.enabled) return null;
  const canClear = hasPermission('printers:clear_plate');
  const count = validPlateCount(plates);

  return (
    <div className="mt-2 rounded-lg border border-bambu-dark-tertiary p-2 text-xs space-y-1.5">
      <div className="flex items-center justify-between gap-2">
        <span className="font-medium text-white">{t('swap.title')}</span>
        <span className="text-bambu-gray">
          {swap.plates_remaining == null
            ? t('swap.platesUnknownLabel')
            : t('swap.platesRemaining', { count: swap.plates_remaining })}
        </span>
      </div>
      {swap.hold_reason && (
        <p className="text-yellow-700 dark:text-yellow-400">{t(swap.hold_reason, { defaultValue: swap.hold_reason })}</p>
      )}
      {canClear && (
        <div className="flex items-center gap-1.5">
          <input
            type="number"
            min={0}
            max={99}
            step={1}
            className={`${INPUT_BASE} w-16 shrink-0`}
            value={plates}
            onChange={(e) => setPlates(e.target.value)}
            aria-label={t('swap.enterPlates')}
          />
          <button
            type="button"
            className={`${BUTTON} flex-1 whitespace-nowrap`}
            disabled={count == null || platesMutation.isPending}
            onClick={() => count != null && platesMutation.mutate(count)}
          >
            {platesMutation.isPending && <Loader2 className="w-3 h-3 animate-spin" />}
            {t('swap.enterPlates')}
          </button>
        </div>
      )}
      {canClear && swap.hold_reason?.startsWith('swap.gate.') && (
        <button
          type="button"
          className={`${BUTTON} w-full`}
          disabled={poseMutation.isPending}
          onClick={() => poseMutation.mutate()}
        >
          {poseMutation.isPending && <Loader2 className="w-3 h-3 animate-spin" />}
          {t('swap.pose')}
        </button>
      )}
      {canClear && swap.hold_reason && swap.hold_reason !== 'swap.hold.stateUnavailable' && (
        <button
          type="button"
          className={`${BUTTON} w-full`}
          disabled={resumeMutation.isPending}
          onClick={() => resumeMutation.mutate()}
        >
          {resumeMutation.isPending && <Loader2 className="w-3 h-3 animate-spin" />}
          {t('swap.resume')}
        </button>
      )}
    </div>
  );
}

type ConfigForm = Record<'marker' | 'exclude_front_mm' | 'exclude_back_mm' | 'bed_depth_mm' | 'clip_min_percent' | 'object_diff_percent', string> & {
  clip_roi: [string, string, string, string];
};

function toForm(c: Partial<SwapConfig>): ConfigForm {
  const s = (v: number | undefined) => (v == null ? '' : String(v));
  const roi = c.clip_roi ?? [];
  return {
    marker: c.marker ?? '',
    exclude_front_mm: s(c.exclude_front_mm),
    exclude_back_mm: s(c.exclude_back_mm),
    bed_depth_mm: s(c.bed_depth_mm),
    clip_min_percent: s(c.clip_min_percent),
    object_diff_percent: s(c.object_diff_percent),
    clip_roi: [s(roi[0]), s(roi[1]), s(roi[2]), s(roi[3])],
  };
}

// A blank or non-numeric field must never become 0 (Number('') === 0 would silently
// remove a no-print zone). It is sent as null instead, which the backend rejects
// with a 422 that the form shows.
function num(v: string): number {
  const n = v.trim() === '' ? NaN : Number(v);
  return (Number.isFinite(n) ? n : null) as number;
}

function fromForm(f: ConfigForm): SwapConfig {
  return {
    marker: f.marker,
    exclude_front_mm: num(f.exclude_front_mm),
    exclude_back_mm: num(f.exclude_back_mm),
    bed_depth_mm: num(f.bed_depth_mm),
    clip_min_percent: num(f.clip_min_percent),
    object_diff_percent: num(f.object_diff_percent),
    clip_roi: f.clip_roi.map(num) as SwapConfig['clip_roi'],
  };
}

// The route needs the Bearer header, so the cropped image is fetched as a blob.
function RecordImage({ printerId, recordId }: { printerId: number; recordId: number }) {
  const { t } = useTranslation();
  const [src, setSrc] = useState<string | null>(null);
  useEffect(() => {
    let url: string | null = null;
    let cancelled = false;
    api
      .getSwapRecordImage(printerId, recordId)
      .then((blob) => {
        if (cancelled) return;
        url = URL.createObjectURL(blob);
        setSrc(url);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
      if (url) URL.revokeObjectURL(url);
    };
  }, [printerId, recordId]);
  if (!src) return <div className="w-16 h-10 shrink-0 rounded bg-bambu-dark-tertiary" />;
  return <img src={src} alt={t('swap.records.image')} className="w-16 h-10 shrink-0 rounded object-cover" />;
}

function aiAnswer(r: SwapRecord, t: (k: string) => string): string {
  const answer = r.ai_object == null ? t('swap.records.aiNone') : r.ai_object ? t('swap.records.aiObject') : t('swap.records.aiClear');
  return r.ai_reason ? `${answer} (${r.ai_reason})` : answer;
}

/** Recent plate-check records with a delete-all button (printers:update). */
export function SwapRecords({ printerId }: { printerId: number }) {
  const { t } = useTranslation();
  const { hasPermission } = useAuth();
  const { showToast } = useToast();
  const queryClient = useQueryClient();
  const allowed = hasPermission('printers:update');
  const { data } = useQuery({
    queryKey: ['swapRecords', printerId],
    queryFn: () => api.getSwapRecords(printerId),
    enabled: allowed,
  });
  const clear = useMutation({
    mutationFn: () => api.deleteSwapRecords(printerId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['swapRecords', printerId] }),
    onError: (e: Error) => showToast(translateErrorMessage(e.message, t).text, 'error'),
  });
  if (!allowed) return null;
  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between gap-2">
        <h4 className="text-xs font-semibold text-white">{t('swap.records.title')}</h4>
        <button
          type="button"
          className={BUTTON}
          disabled={!data?.total || clear.isPending}
          onClick={() => clear.mutate()}
        >
          <Trash2 className="w-3 h-3" />
          {t('swap.records.clear')}
        </button>
      </div>
      {data && data.total === 0 && <p className="text-xs text-bambu-gray">{t('swap.records.empty')}</p>}
      <ul className="space-y-1.5 max-h-64 overflow-y-auto">
        {data?.items.map((r) => (
          <li key={r.id} className="flex items-center gap-2 text-xs text-bambu-gray">
            {r.has_image ? <RecordImage printerId={printerId} recordId={r.id} /> : <div className="w-16 h-10 shrink-0" />}
            <div className="min-w-0">
              <div className="text-white">
                {parseUTCDate(r.created_at)?.toLocaleString()} · {t(`swap.records.outcome.${r.outcome}`)}
              </div>
              <div className="truncate">
                {aiAnswer(r, t)}
                {r.clip_percent != null && ` · ${t('swap.records.clip', { percent: r.clip_percent })}`}
              </div>
            </div>
          </li>
        ))}
      </ul>
      {data && data.total > data.items.length && (
        <p className="text-xs text-bambu-gray">{t('swap.records.showing', { shown: data.items.length, total: data.total })}</p>
      )}
    </div>
  );
}

/** Swap settings inside the Edit Printer dialog (printers:update). Saved separately from the printer fields. */
export function SwapModSettings({ printerId }: { printerId: number }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { data } = useQuery({ queryKey: ['swap', printerId], queryFn: () => api.getSwap(printerId) });
  const [enabled, setEnabled] = useState(false);
  const [form, setForm] = useState<ConfigForm | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (data && form == null) {
      setEnabled(data.enabled);
      setForm(toForm(data.config));
    }
  }, [data, form]);

  const save = useMutation({
    // Only send config when a field changed, so toggling enabled still works while the
    // stored config is invalid (its fields then load blank and would be rejected).
    mutationFn: () => {
      const changed = form && data && JSON.stringify(form) !== JSON.stringify(toForm(data.config));
      return api.updateSwap(printerId, changed ? { enabled, config: fromForm(form) } : { enabled });
    },
    onSuccess: (s) => {
      setError(null);
      queryClient.setQueryData(['swap', printerId], s);
      queryClient.setQueryData(['printerStatus', printerId], (old: PrinterStatus | undefined) =>
        old ? { ...old, swap: { enabled: s.enabled, plates_remaining: s.plates_remaining, hold_reason: s.hold_reason } } : old,
      );
    },
    onError: (e: Error) => setError(e.message),
  });

  if (!form) return null;
  const numField = (key: Exclude<keyof ConfigForm, 'marker' | 'clip_roi'>) => (
    <label className="block">
      <span className="block text-xs text-bambu-gray mb-0.5">{key}</span>
      <input
        type="number"
        step="any"
        className={INPUT}
        value={form[key]}
        onChange={(e) => setForm({ ...form, [key]: e.target.value })}
      />
    </label>
  );

  return (
    <div className="mt-4 pt-4 border-t border-bambu-dark-tertiary space-y-3">
      <h3 className="text-sm font-semibold text-white">{t('swap.settings')}</h3>
      <label className="flex items-center gap-2 text-sm text-white">
        <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} className="rounded" />
        {t('swap.enabled')}
      </label>
      <div className="grid grid-cols-2 gap-2">
        {numField('exclude_front_mm')}
        {numField('exclude_back_mm')}
        {numField('bed_depth_mm')}
        {numField('clip_min_percent')}
        {numField('object_diff_percent')}
      </div>
      <label className="block">
        <span className="block text-xs text-bambu-gray mb-0.5">clip_roi (x, y, w, h)</span>
        <div className="grid grid-cols-4 gap-1">
          {form.clip_roi.map((v, i) => (
            <input
              key={i}
              type="number"
              step="any"
              aria-label={`clip_roi[${i}]`}
              className={INPUT}
              value={v}
              onChange={(e) => {
                const roi = [...form.clip_roi] as ConfigForm['clip_roi'];
                roi[i] = e.target.value;
                setForm({ ...form, clip_roi: roi });
              }}
            />
          ))}
        </div>
      </label>
      <label className="block">
        <span className="block text-xs text-bambu-gray mb-0.5">marker</span>
        <input type="text" className={INPUT} value={form.marker} onChange={(e) => setForm({ ...form, marker: e.target.value })} />
      </label>
      {error && <p className="text-xs text-red-500">{error}</p>}
      <button
        type="button"
        onClick={() => save.mutate()}
        disabled={save.isPending}
        className="w-full px-3 py-2 rounded-lg bg-bambu-green text-white text-sm font-medium hover:bg-bambu-green/80 disabled:opacity-50"
      >
        {save.isPending ? <Loader2 className="w-4 h-4 animate-spin inline" /> : t('common.save')}
      </button>
      <SwapRecords printerId={printerId} />
    </div>
  );
}
