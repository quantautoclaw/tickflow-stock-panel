import { useQuery } from '@tanstack/react-query'
import { Activity, TrendingUp, Wallet } from 'lucide-react'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { fmtPrice, fmtPct } from '@/lib/format'
import { cn } from '@/lib/cn'

const ACTION_STYLE: Record<string, string> = {
  EXIT:         'bg-danger/10 text-danger border-danger/20',
  REDUCE:       'bg-warning/10 text-warning border-warning/20',
  TAKE_PROFIT:  'bg-bull/10 text-bull border-bull/20',
  ADD:          'bg-accent/10 text-accent border-accent/20',
  HOLD:         'bg-elevated text-muted border-border/40',
}

/**
 * QuantX 只读代理面板 — 持仓 + 盘中决策建议, 只读展示。
 *
 * 边界: 不提供任何下单/撤单操作入口。QuantX 未配置 (QUANTX_API_BASE_URL 为空)
 * 或服务不可达时组件返回 null, 不占用监控中心的页面空间/不打扰未使用实盘的用户。
 */
export function QuantxMonitorPanel() {
  const statusQuery = useQuery({
    queryKey: QK.quantxStatus,
    queryFn: () => api.quantxStatus(),
    refetchInterval: 30_000,
    retry: false,
  })

  const available = statusQuery.data?.available === true

  const positionsQuery = useQuery({
    queryKey: QK.quantxPositions,
    queryFn: () => api.quantxPositions(),
    enabled: available,
    refetchInterval: 15_000,
    retry: false,
  })

  const decisionsQuery = useQuery({
    queryKey: QK.quantxDecisions,
    queryFn: () => api.quantxDecisions({ minutes: 240 }),
    enabled: available,
    refetchInterval: 15_000,
    retry: false,
  })

  // 未配置 QuantX 实盘只读代理 / 服务不可达: 整块隐藏, 不打扰未使用该功能的用户。
  if (statusQuery.isLoading || !available) return null

  const positions = positionsQuery.data?.available ? (positionsQuery.data.positions ?? []) : []
  const asset = positionsQuery.data?.available ? positionsQuery.data.asset : null
  const decisions = decisionsQuery.data?.available ? (decisionsQuery.data.decisions ?? []) : []

  return (
    <section className="mb-4 flex-shrink-0 overflow-hidden rounded-xl border border-border bg-surface/40 shadow-lg shadow-black/5">
      <div className="flex items-center gap-2 border-b border-border/60 bg-surface/60 px-4 py-2">
        <Activity className="h-3.5 w-3.5 text-accent" />
        <h3 className="text-xs font-semibold text-foreground">QuantX 实盘只读</h3>
        <span className="rounded-md bg-elevated/50 px-1.5 py-0.5 text-[10px] font-medium text-muted">
          持仓/决策展示 · 不代理下单
        </span>
        {asset && (
          <span className="ml-auto text-[11px] text-secondary">
            总资产 <span className="font-mono text-foreground">{fmtPrice(asset.total_asset, 0)}</span>
            <span className="mx-1 text-muted">·</span>
            可用 <span className="font-mono text-foreground">{fmtPrice(asset.cash, 0)}</span>
          </span>
        )}
      </div>

      <div className="grid grid-cols-1 gap-0 divide-y divide-border/40 lg:grid-cols-2 lg:divide-x lg:divide-y-0">
        {/* 持仓 */}
        <div className="max-h-48 overflow-auto p-3">
          <div className="mb-1.5 flex items-center gap-1.5 text-[11px] font-medium text-secondary">
            <Wallet className="h-3 w-3" />持仓 ({positions.length})
          </div>
          {positions.length === 0 ? (
            <div className="py-4 text-center text-[11px] text-muted">暂无持仓</div>
          ) : (
            <div className="space-y-1">
              {positions.map(p => (
                <div key={p.stock_code} className="flex items-center justify-between rounded-md bg-elevated/40 px-2 py-1 text-[11px]">
                  <div className="min-w-0 flex items-center gap-1.5">
                    <span className="font-mono text-foreground">{p.stock_code}</span>
                    <span className="truncate text-muted">{p.name}</span>
                  </div>
                  <div className="flex shrink-0 items-center gap-2 font-mono">
                    <span className="text-muted">{p.volume}股</span>
                    <span className={cn(p.unrealized_pnl >= 0 ? 'text-bull' : 'text-bear')}>
                      {p.unrealized_pnl >= 0 ? '+' : ''}{fmtPrice(p.unrealized_pnl, 0)}
                    </span>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* 盘中决策建议 */}
        <div className="max-h-48 overflow-auto p-3">
          <div className="mb-1.5 flex items-center gap-1.5 text-[11px] font-medium text-secondary">
            <TrendingUp className="h-3 w-3" />盘中决策建议 ({decisions.length})
          </div>
          {decisions.length === 0 ? (
            <div className="py-4 text-center text-[11px] text-muted">近 4 小时无建议</div>
          ) : (
            <div className="space-y-1.5">
              {decisions.map((d, i) => (
                <div key={`${d.symbol}-${i}`} className="rounded-md bg-elevated/40 px-2 py-1.5 text-[11px]">
                  <div className="flex items-center gap-1.5">
                    <span className={cn('shrink-0 rounded px-1 py-0.5 text-[9px] font-semibold border', ACTION_STYLE[d.action] ?? 'bg-elevated text-muted border-border/40')}>
                      {d.action}
                    </span>
                    <span className="font-mono text-foreground">{d.symbol}</span>
                    {typeof d.pnl_pct === 'number' && (
                      <span className={cn('ml-auto font-mono', d.pnl_pct >= 0 ? 'text-bull' : 'text-bear')}>
                        {fmtPct(d.pnl_pct)}
                      </span>
                    )}
                  </div>
                  {(d.llm_note || d.reasons) && (
                    <div className="mt-0.5 truncate text-muted">{d.llm_note || d.reasons}</div>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </section>
  )
}
