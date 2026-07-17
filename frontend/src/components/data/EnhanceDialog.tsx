import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Loader2, Sparkles, X } from 'lucide-react'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { toast } from '@/components/Toast'

const ASSETS = [
  { key: 'stock', label: '股票' },
  { key: 'etf', label: 'ETF' },
  { key: 'index', label: '指数' },
] as const

function defaultRange(): { start: string; end: string } {
  const end = new Date()
  const start = new Date()
  start.setFullYear(start.getFullYear() - 1)
  const fmt = (d: Date) => d.toISOString().slice(0, 10)
  return { start: fmt(start), end: fmt(end) }
}

export function EnhanceDialog({
  open,
  onClose,
  onStarted,
}: {
  open: boolean
  onClose: () => void
  onStarted: (jobId: string) => void
}) {
  const qc = useQueryClient()
  // 取可用的增强源插件 (role=enhancer/both 且 available)
  const sources = useQuery({ queryKey: QK.dataSources, queryFn: api.dataSources })
  const enhancers = useMemo(
    () => (sources.data?.plugins ?? []).filter(p => (p.role === 'enhancer' || p.role === 'both') && p.available),
    [sources.data],
  )
  const [provider, setProvider] = useState('')
  const [range, setRange] = useState(defaultRange)
  const [assets, setAssets] = useState<Set<string>>(new Set(['stock', 'etf', 'index']))

  const effectiveProvider = provider || enhancers[0]?.name || ''
  const startMut = useMutation({
    mutationFn: () =>
      api.enhanceData(effectiveProvider, range.start, range.end, [...assets]),
    onSuccess: ({ job_id, reused }) => {
      toast(reused ? '已有任务在运行, 复用中' : '增强任务已启动', 'success')
      qc.invalidateQueries({ queryKey: QK.pipelineJobs })
      onStarted(job_id)
      onClose()
    },
    onError: (e: unknown) => toast(`启动失败: ${(e as Error).message || e}`, 'error'),
  })

  if (!open) return null

  const toggleAsset = (k: string) => {
    const next = new Set(assets)
    if (next.has(k)) next.delete(k); else next.add(k)
    setAssets(next)
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4" onClick={onClose}>
      <div
        className="w-full max-w-md rounded-card border border-border bg-surface p-5 shadow-xl"
        onClick={e => e.stopPropagation()}
      >
        <div className="flex items-center justify-between mb-4">
          <div className="flex items-center gap-2">
            <Sparkles className="h-4 w-4 text-accent" />
            <h3 className="text-sm font-semibold text-foreground">历史数据补全</h3>
          </div>
          <button onClick={onClose} className="text-muted hover:text-foreground"><X className="h-4 w-4" /></button>
        </div>

        {enhancers.length === 0 ? (
          <p className="text-xs text-muted/70 mb-2">
            暂无可用增强源。请先在「设置 → 数据源 → 数据增强」启用 QuantX。
          </p>
        ) : (
          <div className="space-y-4">
            <div>
              <label className="block text-[11px] text-muted mb-1.5">增强源</label>
              <select
                value={effectiveProvider}
                onChange={e => setProvider(e.target.value)}
                className="w-full rounded-btn border border-border bg-elevated/30 px-2.5 py-1.5 text-xs text-foreground"
              >
                {enhancers.map(p => (
                  <option key={p.name} value={p.name}>{p.display_name}</option>
                ))}
              </select>
            </div>

            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className="block text-[11px] text-muted mb-1.5">开始日期</label>
                <input
                  type="date"
                  value={range.start}
                  onChange={e => setRange(r => ({ ...r, start: e.target.value }))}
                  className="w-full rounded-btn border border-border bg-elevated/30 px-2.5 py-1.5 text-xs text-foreground"
                />
              </div>
              <div>
                <label className="block text-[11px] text-muted mb-1.5">结束日期</label>
                <input
                  type="date"
                  value={range.end}
                  onChange={e => setRange(r => ({ ...r, end: e.target.value }))}
                  className="w-full rounded-btn border border-border bg-elevated/30 px-2.5 py-1.5 text-xs text-foreground"
                />
              </div>
            </div>

            <div>
              <label className="block text-[11px] text-muted mb-1.5">资产类型</label>
              <div className="flex gap-2">
                {ASSETS.map(a => (
                  <button
                    key={a.key}
                    onClick={() => toggleAsset(a.key)}
                    className={`px-2.5 py-1 rounded-btn text-xs transition-colors ${
                      assets.has(a.key)
                        ? 'bg-accent/15 text-accent border border-accent/30'
                        : 'bg-elevated/30 text-muted border border-border'
                    }`}
                  >
                    {a.label}
                  </button>
                ))}
              </div>
            </div>

            <div className="flex items-start gap-1.5 text-[10px] text-muted/60 bg-elevated/20 rounded p-2">
              <span>仅补缺, 不覆盖已有 TickFlow 数据; 单次跨度上限 5 年。</span>
            </div>

            <div className="flex justify-end gap-2 pt-1">
              <button onClick={onClose} className="px-3 py-1.5 rounded-btn text-xs text-muted hover:text-foreground">
                取消
              </button>
              <button
                onClick={() => startMut.mutate()}
                disabled={startMut.isPending || assets.size === 0 || !range.start || !range.end}
                className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-btn bg-accent/15 text-accent hover:bg-accent/25 text-xs font-medium disabled:opacity-40 transition-colors"
              >
                {startMut.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Sparkles className="h-3.5 w-3.5" />}
                开始补全
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
