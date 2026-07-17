"""盘后管道 API — 异步触发 + 进度跟踪。"""
from __future__ import annotations

import asyncio
import concurrent.futures as _cf
import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.api.data import invalidate_storage_cache
from app.jobs import daily_pipeline
from app.services.pipeline_jobs import job_store, release_run_slot, try_acquire_run_slot

# 长时间任务专用线程池（隔离于 FastAPI 默认线程池，防止阻塞请求处理）
_long_task_executor = _cf.ThreadPoolExecutor(max_workers=2, thread_name_prefix="long-task")
_background_tasks: set[asyncio.Task] = set()


def _spawn_background(coro) -> None:
    """保留 task 强引用直到结束, 避免后台任务被提前回收。"""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/pipeline", tags=["pipeline"])


@router.post("/run")
async def run_now(request: Request) -> dict:
    """异步触发盘后管道,立即返回 job_id。客户端轮询 /jobs/{id} 拿进度。

    若已有任务在跑,**返回该任务 id 而不是开新任务**(防止并发拉数据撞限流)。
    但如果该任务已运行超过 10 分钟 (可能因 reload 卡死), 强制标记为失败后重新创建。
    """
    repo = request.app.state.repo
    capset = request.app.state.capabilities

    # 检测卡死的 running job (如 reload 后孤儿 task / 网络读无限阻塞)。
    # reap_stale 会在 /run 和 /jobs/{id} 轮询端点都调用,保证卡死后能自愈。
    job_store.reap_stale()

    # 单飞: 复用任何活跃 (pending∨running) 任务, is_new=False 时不再调度新任务
    job_id, is_new = job_store.create()
    if not is_new:
        return {"job_id": job_id, "reused": True}

    # 在 executor 里跑同步任务(pipeline 内部都是阻塞 IO + CPU)
    async def task() -> None:
        # 重任务执行槽: 防僵尸并发(reap 后线程仍活时新任务不得并行写 parquet)
        if not try_acquire_run_slot():
            job_store.fail(job_id, "已有数据任务在运行(或上一次任务卡死未结束),请稍后再试")
            return
        # 管道运行期间暂停实时行情取数, 防止覆写同一批 parquet 竞态
        qs = getattr(request.app.state, "quote_service", None)
        try:
            job_store.start(job_id)
            loop = asyncio.get_event_loop()

            def progress(stage: str, pct: int, msg: str, stage_pct: int | None = None,
                         skip_log: bool = False) -> None:
                job_store.progress(job_id, stage, pct, msg, stage_pct=stage_pct, skip_log=skip_log)

            def _run() -> dict:
                if qs:
                    with qs.paused():
                        return daily_pipeline.run_now(repo, capset, on_progress=progress)
                return daily_pipeline.run_now(repo, capset, on_progress=progress)

            result = await loop.run_in_executor(_long_task_executor, _run)
            job_store.succeed(job_id, result)
            invalidate_storage_cache()
            repo.refresh_cache()  # 刷新 Polars 缓存
        except Exception as e:
            logger.exception("pipeline failed")
            job_store.fail(job_id, str(e))
            invalidate_storage_cache()
        finally:
            release_run_slot()

    _spawn_background(task())
    return {"job_id": job_id, "reused": False}


@router.get("/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    # 每次轮询都检查卡死 job — 前端每秒轮询,STALE_JOB_TIMEOUT_S(10min)后必定自愈,
    # 无需用户再次手动点「同步」。
    job_store.reap_stale()
    j = job_store.get(job_id)
    if not j:
        raise HTTPException(status_code=404, detail="job not found")
    return j


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict:
    """手动取消一个 running 的 job。"""
    j = job_store.get(job_id)
    if not j:
        raise HTTPException(status_code=404, detail="job not found")
    if j["status"] not in ("running", "pending"):
        raise HTTPException(status_code=400, detail=f"job status is {j['status']}, cannot cancel")
    job_store.fail(job_id, "用户手动取消")
    return {"cancelled": job_id}


@router.get("/jobs")
def list_jobs(limit: int = 20) -> dict:
    return {
        "active_id": job_store.active_id(),
        "jobs": job_store.list_recent(limit=limit),
    }


# ================================================================
# 手动历史增强 (§13)
# ================================================================

class EnhanceIn(BaseModel):
    provider: str
    start_date: str
    end_date: str
    asset_types: list[str] = Field(
        default_factory=lambda: ["stock", "etf", "index"]
    )
    conflict: str = "keep_existing"


@router.post("/enhance")
async def enhance(request: Request, req: EnhanceIn) -> dict:
    """异步触发历史数据增强(补缺, 不覆盖主数据)。立即返回 job_id。

    约束:
      - provider 必须是已加载 enhancer。
      - start_date <= end_date。
      - 首期只允许 conflict=keep_existing。
    与盘后任务共享全局写入槽, 禁止并发修改 Parquet。
    """
    from datetime import date as _date

    from app.data_providers import custom as custom_sources
    from app.services.data_enhancement import EnhancementRequest, run_enhancement

    repo = request.app.state.repo
    try:
        start = _date.fromisoformat(req.start_date)
        end = _date.fromisoformat(req.end_date)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"日期格式错误: {e}") from e
    if start > end:
        raise HTTPException(status_code=400, detail="start_date 不能晚于 end_date")
    if req.conflict != "keep_existing":
        raise HTTPException(status_code=400, detail="首期只允许 conflict=keep_existing")
    # 校验 provider 是已加载 enhancer
    manifest = next((p for p in custom_sources.list_plugins() if p["name"] == req.provider), None)
    if manifest is None or str(manifest.get("role", "")).lower() not in {"enhancer", "both"}:
        raise HTTPException(status_code=400, detail=f"'{req.provider}' 不是已注册的增强源")
    if not manifest.get("available"):
        raise HTTPException(status_code=400, detail=f"'{req.provider}' 当前不可用: {manifest.get('status', '')}")
    # 最大跨度 5 年
    if (end - start).days > 365 * 5:
        raise HTTPException(status_code=400, detail="单次跨度不得超过 5 年, 请分批执行")

    try:
        enhancement_req = EnhancementRequest(
            provider=req.provider,
            start_date=start,
            end_date=end,
            asset_types=tuple(req.asset_types),
            conflict=req.conflict,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    # 单飞: 复用活跃任务
    job_store.reap_stale()
    job_id, is_new = job_store.create()
    if not is_new:
        return {"job_id": job_id, "reused": True}

    async def task() -> None:
        if not try_acquire_run_slot():
            job_store.fail(job_id, "已有数据任务在运行, 请稍后再试")
            return
        try:
            job_store.start(job_id)
            loop = asyncio.get_event_loop()

            def progress(stage: str, pct: int, msg: str, stage_pct: int | None = None,
                         skip_log: bool = False) -> None:
                job_store.progress(job_id, stage, pct, msg, stage_pct=stage_pct, skip_log=skip_log)

            summary = await loop.run_in_executor(
                _long_task_executor,
                lambda: run_enhancement(repo, enhancement_req, on_progress=progress),
            )
            job_store.succeed(job_id, {
                "provider": summary.provider,
                "inserted_rows": summary.inserted_rows,
                "affected_symbols": summary.affected_symbols[:50],
                "assets": summary.assets,
            })
            invalidate_storage_cache()
            repo.refresh_cache()
        except Exception as e:
            logger.exception("enhance failed")
            job_store.fail(job_id, str(e))
            invalidate_storage_cache()
        finally:
            release_run_slot()

    _spawn_background(task())
    return {"job_id": job_id, "reused": False}
