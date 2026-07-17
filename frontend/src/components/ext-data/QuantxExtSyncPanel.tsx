import { useState } from 'react'
import { Loader2, Zap, CheckCircle2 } from 'lucide-react'
import { api, type ExtDataConfig } from '@/lib/api'
import { toast } from '@/components/Toast'

/** QuantX 扩展表 (资金流/涨停明细/龙虎榜) 唯一预设 id, 与 backend/app/services/quantx_ext_tables.py 保持一致。 */
export const QUANTX_EXT_PRESET_IDS = ['ext_moneyflow_qx', 'ext_limit_list_qx', 'ext_top_list_qx']

/** 数据来自本地 QuantX DataStore (非网络拉取), 与通用 URL 拉取表单 (ExtDataPullPanel) 不同,
 * 只需一个「同步最近 N 天」按钮, 不需要 URL/Headers/字段映射等配置。 */
export function QuantxExtSyncPanel({ config, onSynced }: {
  config: ExtDataConfig
  onSynced: () => void
}) {
  const [days, setDays] = useState(7)
  const [syncing, setSyncing] = useState(false)
  const [result, setResult] = useState<{ rows: number; last_date: string } | null>(null)
  const [error, setError] = useState('')

  const handleSync = () => {
    setSyncing(true); setError(''); setResult(null)
    api.extDataQuantxSync(config.id, days)
      .then(r => {
        setResult({ rows: r.rows, last_date: r.last_date })
        onSynced()
        toast(r.rows > 0 ? `同步成功 · ${r.rows} 行` : '同步完成 · 该区间无新数据', 'success')
      })
      .catch(e => setError(e.message || '同步失败 (请检查设置页 QuantX 插件是否已配置)'))
      .finally(() => setSyncing(false))
  }

  return (
    <div className="space-y-3">
      <div className="text-[11px] text-secondary leading-relaxed">
        数据来源: QuantX DataStore (本地读取, 非网络拉取)。按 (symbol, date) 写入, 不影响其余扩展表。
        盘后管道启用 QuantX 增强源时会自动增量同步最近数日; 这里可手动回补更长区间。
      </div>

      <div>
        <div className="text-[10px] text-muted mb-1">同步最近 N 天</div>
        <input
          type="number" min={1} max={365} value={days}
          onChange={e => setDays(Math.max(1, Math.min(365, Number(e.target.value) || 1)))}
          className="w-full rounded-btn border border-border bg-elevated px-2.5 py-1.5 text-[11px] font-mono text-foreground"
        />
      </div>

      <button
        onClick={handleSync}
        disabled={syncing}
        className="w-full inline-flex items-center justify-center gap-1 py-2 rounded-btn bg-accent/90 text-base text-xs font-medium hover:bg-accent disabled:opacity-40 transition-colors"
      >
        {syncing ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Zap className="h-3.5 w-3.5" />}
        立即同步
      </button>

      {result && (
        <div className="rounded-card border border-emerald-500/30 bg-emerald-500/[0.06] p-2.5 flex items-center justify-between text-[10px]">
          <span className="text-emerald-500 font-medium flex items-center gap-1">
            <CheckCircle2 className="h-3 w-3" />同步成功
          </span>
          <span className="text-secondary">{result.rows} 行{result.last_date ? ` · 最新 ${result.last_date}` : ''}</span>
        </div>
      )}

      {error && (
        <div className="text-[10px] text-danger text-center bg-danger/[0.06] rounded-btn py-1.5">
          {error}
        </div>
      )}
    </div>
  )
}
