import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Activity, ChevronDown, ChevronUp, History } from 'lucide-react'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { MarkdownRenderer } from '@/components/financials/MarkdownRenderer'

/**
 * QuantX 研究数据只读展示 — 大盘复盘报告 + 策略回测运行历史。
 *
 * 只读加载 QuantX 已产出的内容(不触发新的 LLM 生成/回测)。QuantX 未配置
 * (QUANTX_API_BASE_URL 为空)或数据缺失时组件返回 null, 不占用复盘页空间。
 * 与 TickFlow 自有的「生成复盘」功能相互独立, 互不覆盖 —— 这里是「第二意见」,
 * 不是替代。
 */
export function QuantxReviewCard() {
  const [expanded, setExpanded] = useState(false)

  const reviewQuery = useQuery({
    queryKey: QK.quantxMarketReview,
    queryFn: () => api.quantxMarketReview(),
    retry: false,
    staleTime: 5 * 60_000,
  })

  const runsQuery = useQuery({
    queryKey: ['quantx-runs-recent'],
    queryFn: () => api.quantxRuns({ limit: 5 }),
    retry: false,
    staleTime: 5 * 60_000,
  })

  const reviewAvailable = reviewQuery.data?.available === true && !!reviewQuery.data.report
  const runsAvailable = runsQuery.data?.available === true && (runsQuery.data.runs?.length ?? 0) > 0

  if (reviewQuery.isLoading || (!reviewAvailable && !runsAvailable)) return null

  return (
    <section className="rounded-card border border-border bg-surface/60 shadow-sm">
      <button
        onClick={() => setExpanded(v => !v)}
        className="flex w-full items-center gap-2 px-4 py-2.5 text-left"
      >
        <Activity className="h-3.5 w-3.5 text-accent" />
        <h3 className="text-xs font-semibold text-foreground">QuantX 研究参考</h3>
        <span className="rounded-md bg-elevated/50 px-1.5 py-0.5 text-[10px] font-medium text-muted">只读 · 第二意见</span>
        <span className="ml-auto text-muted">
          {expanded ? <ChevronUp className="h-3.5 w-3.5" /> : <ChevronDown className="h-3.5 w-3.5" />}
        </span>
      </button>

      {expanded && (
        <div className="space-y-3 border-t border-border/50 px-4 py-3">
          {reviewAvailable && (
            <div>
              <div className="mb-1.5 text-[11px] font-medium text-secondary">
                大盘复盘 {reviewQuery.data?.date ? `· ${reviewQuery.data.date}` : ''}
              </div>
              <div className="rounded-lg bg-elevated/30 p-3 text-xs">
                <MarkdownRenderer content={reviewQuery.data!.report!} />
              </div>
            </div>
          )}

          {runsAvailable && (
            <div>
              <div className="mb-1.5 flex items-center gap-1.5 text-[11px] font-medium text-secondary">
                <History className="h-3 w-3" />最近回测运行
              </div>
              <div className="space-y-1">
                {(runsQuery.data?.runs ?? []).map((r, i) => (
                  <div key={i} className="flex items-center justify-between rounded-md bg-elevated/40 px-2 py-1 text-[11px]">
                    <span className="truncate text-foreground">{String(r.strategy_name ?? r.run_id ?? '—')}</span>
                    <span className="shrink-0 font-mono text-muted">{String(r.run_at ?? r.created_at ?? '')}</span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </section>
  )
}
