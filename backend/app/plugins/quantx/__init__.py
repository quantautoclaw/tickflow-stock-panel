"""QuantX 数据增强插件。

定位: **可发现、可配置、可关闭、可测试的行情增强插件**。
TickFlow 保持主数据源, QuantX 按 (symbol, date) 补缺; 实时行情按 provider chain 获取。

- 历史日K: 仅补缺 (keep_existing), 不覆盖 TickFlow 已有数据。
- 日K统一以「不复权 OHLCV」进入 pipeline, 复权只算一次 (由现有 indicators.pipeline 完成)。
- 实时行情: 通过 HttpQuoteChain 多源择优, 可作为优先源。
- 维表: 字段级 coalesce, 不破坏 TickFlow 字段 (股本/涨跌停等)。

QuantX 项目代码零修改, 仅通过 quantx_data public API 读取。
"""
