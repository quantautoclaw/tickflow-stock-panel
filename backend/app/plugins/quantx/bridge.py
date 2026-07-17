"""QuantX import / 资源管理桥接。

职责:
  - 处理 QuantX import (直接 import 或临时把 QUANTX_PACKAGES_PATH 加入 sys.path)。
  - 创建 QuantX DataStore 单例 (惰性, availability 检查不读全市场数据)。
  - 创建 HttpQuoteChain 单例 (实时多源择优)。
  - availability 检查 (清晰错误原因) + 资源释放。

线程模型: 单例惰性创建, close() 释放 HttpQuoteChain 后台线程池。
幂等: 多次调用 get_store()/get_http_quote_chain() 返回同一实例。
"""
from __future__ import annotations

import logging
import sys
import threading

from app.config import settings

logger = logging.getLogger(__name__)

_lock = threading.RLock()
_imported = False
_store = None
_http_chain = None


def _ensure_import() -> None:
    """把 QuantX ``packages/`` 注入 sys.path 并 import quantx_data。幂等、线程安全。"""
    global _imported
    if _imported:
        return
    with _lock:
        if _imported:
            return
        # 优先尝试直接 import (环境已安装 quantx_data 时无需配置 packages 路径)
        try:
            import quantx_data
            _imported = True
            logger.info("quantx bridge: quantx_data 已加载 (环境内置)")
            return
        except ImportError:
            pass
        # 直接 import 失败 → 用 QUANTX_PACKAGES_PATH
        pkg = settings.quantx_packages_path
        pkg_str = str(pkg) if pkg and str(pkg) != "." else ""
        if not pkg_str:
            raise ImportError(
                "未配置 QUANTX_PACKAGES_PATH, 且当前 Python 环境无法 import quantx_data"
            )
        if pkg_str not in sys.path:
            sys.path.insert(0, pkg_str)
        try:
            import quantx_data  # noqa: F401
        except ImportError as e:
            raise ImportError(
                f"QUANTX_PACKAGES_PATH={pkg_str} 下无法 import quantx_data: {e}"
            ) from e
        _imported = True
        logger.info("quantx bridge: quantx_data 已加载 (sys.path += %s)", pkg_str)


def availability() -> tuple[bool, str]:
    """检查 QuantX 是否可用, 返回 (是否可用, 原因)。

    检查顺序:
      1. 能否 import quantx_data (直接或经 QUANTX_PACKAGES_PATH)。
      2. QUANTDATA_ROOT 已配置且目录存在。
      3. 能否创建 DataStore (不读全市场数据)。
    任何步骤失败返回 (False, 清晰原因); 全部通过返回 (True, "ok")。
    """
    try:
        _ensure_import()
    except ImportError as e:
        return False, str(e)

    root = settings.quantx_data_root
    root_str = str(root) if root and str(root) != "." else ""
    if not root_str:
        return False, "未配置 QUANTDATA_ROOT"
    try:
        if not root.exists():
            return False, f"QUANTDATA_ROOT 不存在: {root_str}"
    except OSError as e:
        return False, f"QUANTDATA_ROOT 路径异常: {e}"

    # 验证 DataStore 可创建 (构造时不读全市场数据)
    try:
        get_store()
    except Exception as e:
        return False, f"QuantX 初始化失败: {e}"
    return True, "ok"


def get_store():
    """惰性创建 QuantX DataStore 单例。available 时返回实例。"""
    global _store
    if _store is None:
        with _lock:
            if _store is None:
                _ensure_import()
                from quantx_data import get_store as _get_store
                root = settings.quantx_data_root
                root_str = str(root) if root and str(root) != "." else ""
                if not root_str:
                    raise RuntimeError("QUANTDATA_ROOT 未配置")
                _store = _get_store(root_str)
                logger.info("quantx bridge: DataStore -> %s", root_str)
    return _store


def get_http_quote_chain():
    """惰性创建 HttpQuoteChain 单例 (新浪/腾讯/akshare 并行择优 + 熔断)。

    启动失败返回 None (调用方回退 TickFlow)。后台探测线程为 daemon。
    """
    global _http_chain
    if _http_chain is None:
        with _lock:
            if _http_chain is None:
                try:
                    _ensure_import()
                    from quantx_data.http_quotes import HttpQuoteChain
                    _http_chain = HttpQuoteChain()
                    logger.info("quantx bridge: HttpQuoteChain 已启用 (多源实时行情)")
                except ImportError as e:
                    logger.warning("quantx bridge: HttpQuoteChain 初始化失败 (import): %s", e)
                except Exception as e:
                    logger.warning("quantx bridge: HttpQuoteChain 初始化失败: %s", e)
    return _http_chain


def close() -> None:
    """释放资源: 停止 HttpQuoteChain 后台线程池。

    测试 reload / 进程退出时调用, 避免遗留线程。
    DataStore 无需显式释放 (纯内存 LRU 缓存)。
    """
    global _http_chain, _store
    with _lock:
        chain = _http_chain
        _http_chain = None
        _store = None
    if chain is not None:
        try:
            chain.stop()
        except Exception as e:
            logger.debug("quantx bridge: HttpQuoteChain.stop() error: %s", e)
