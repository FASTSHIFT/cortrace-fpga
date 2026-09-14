# Proposal 33 — RETIRED（回到上游最简方案）

**日期**：2026-07-12  
**状态**：**RETIRED / do-not-implement**

## 为何撤回

原提案主张给 `trace_capture_direct.v` 每根数据 lane 加一个 IDELAYE2，走 CSR-driven 32-tap 校准来把 30 % 的"bit6 错采率"降下来。已经落地一次完整编译 + 上板实测，发现方向是错的：

1. **上游根本没这么做**。`orbtrace/orbtrace/trace/glue.py` 的 `TraceIO` 一共 15 行：`DDRInput(clk=traceclk, i=tracedata[i], o1=trace_a[i], o2=trace_b[i])` × 4 + `ClockSignal().eq(traceclk)`。没有 IDELAY / IDELAYCTRL / REFCLK / tap。
2. **我们看到的 "30 % header 错误率" 是 PC 端的假象**。当时我们在 PC 端用 `parity × order` 4 种组合猜 nibble 边界，选到了错的一组还看不出来。把 4-bit 的 `WIDTH==4` 分支从"raw byte + PC 端猜"改成**和 2-bit 一样走 FPGA 内 `traceIF`（自动 TPIU frame sync + tpiu_demux）**，PC 端就没有猜的余地了，参照 orbtrace 上游一致。
3. **加了 IDELAY 反而扰动 DDR 相位**：baseline tap=16 时错误率反而抖动到 17-22 %，且 A-sync 数量从 13 掉到 1。不是 skew 问题，是采样窗被 IDELAY 挪了 1.25 ns 之后离 IDDR 边沿更近。

## 真正的 fix（比原方案小得多）

- 一次 RTL 改动：`trace_mmcm_stream_top.v` 里 4-bit 路径不再走 `g_raw`，改走 `g_traceif` （唯一变量是 `traceif_width = 2'b11`）。
- 一次 PC 端简化：不再做 parity/order 4 组搜索。tpiu_demux 输出的 ETM 字节就是最终的 ETMv4 字节流。

## 教训

- 上游 orbtrace 的方案已经在多款 Cortex-M（M4/M7 STM32F/H 系列）跑通过；先看上游做没做，再决定要不要额外发明轮子。
- FPGA 内做 TPIU frame sync 是关键——一次锁定，无相位歧义；PC 端做 nibble 组装会遇到 4-组解空间，加复杂度还不可靠。

原提案的诊断部分（Atom 后 30 % 头字节非法、bit6 特定翻转……）**都是 PC 端搜索走错通道**得来的伪信号；一旦 FPGA 侧 tpiu_demux 参与，这些"错误"就自然消失。
