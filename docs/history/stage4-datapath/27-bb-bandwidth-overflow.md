# 27 — BB=1 溢出 vs BB=0 太稀疏：ETM 带宽是那 17% 的真凶

日期：2026-09-07

## 发现：那 ~17% illegal-atom 不是 PC deframe bug，是 ETM 源头溢出

之前一直往"PC 端 deframe 有 bug"找那 17%，**方向错了**。真凶在 STM32 的 ETM：

- **TPIU 出口带宽**：56.25MHz pin × DDR × 4 lane = **450 Mbit/s**（物理天花板，SI 限制在 ~56MHz，见 doc 23）。
- **BB=1（BranchBroadcast）产生率**：150MHz CPU + 高分支密度（det_iter→node→leaf，几条指令一个分支），每个分支广播完整目标地址 → ETM 产生率 **超过 450 Mbit/s 出口**。
- 结果：ETF（4KB FIFO）填满 → 即使 STALL 使能，瞬时爆发仍 **Overflow 丢数据**。

**实测铁证**：BB=1 抓 1.9MB 解码，出现 **27 个 `I_OVERFLOW` 包**。每次 overflow 后 ETM 要重新同步（A-Sync→TraceInfo→TraceOn），中间断流 → 解码器碰到断点报 illegal-atom。这完全解释了 17%，也和"TPIU 测试图案 100% 干净（采集链无错）"自洽——**问题在源头 ETM 溢出，不在采集链、不在 deframe**。

寄存器实测：TRCCONFIGR=0x9（BB 开 bit3），TRCSTALLCTLR=0x10C（STALL 开）。STALL 是
尽力而为，不保证零溢出（ARM IHI0064，AGENT.md §23）。

## 但 BB=0 又太稀疏（另一个极端）

关 BB（TRCCONFIGR=0x1）后重测：

| | BB=1 | BB=0 |
|---|---|---|
| I_OVERFLOW / 1.9MB | 27 | 5 |
| ETM 字节 / 1.9MB raw | 348 KB | 34 KB |
| 原始流 HSYNC 填充占比 | 少 | **83%**（0xFF 41.5% + 0x7F 41.5%）|
| A-sync | 86 | 4 |
| 解码 instr-range | 152 | 0（地址全乱：0x92C0…）|

BB=0 把产生率压到远低于出口（好），但**太稀疏**：小循环几乎不产生 trace，TPIU 83% 在发
HSYNC 填充，A-sync 稀少（TRCSYNCPR=2^12）。没有 BB 的全地址广播，解码器只在周期性
I-sync 点知道绝对 PC，点之间无法定位 → 解出垃圾地址。

nibble 相位不是问题（parity=1/order=0 明确最优，picker 选对了），是数据本身太少。

## 结论：这是真实的带宽权衡，不是简单开关

- BB=1：数据丰富、能解出真实函数流，但**溢出丢包**（27 次）→ 17% illegal-atom。
- BB=0：不溢出，但**太稀疏**、解码器缺绝对地址 → 解不出流程。

两个极端都不理想。要 100% 无损**且**可解码，需要让 ETM 产生率落在
"< 450Mbit/s 出口"**且**"足够密到能维持解码上下文"的中间带。候选手段（未验证）：

1. **BB=1 + 降 CPU 频率**：降 sysclk 让分支率下降，把 BB 产生率压到出口以下，同时保留
   全地址广播的可解码性。最有希望——直接对症（产生率超出口）。
2. **BB=0 + 提高 sync 频率（TRCSYNCPR 调小）**：让 A-sync 更密，缩短解码器"失联"窗口。
3. **BB=1 + 缩短循环/加 workload**：改变分支密度分布。
4. 提高 TPIU 出口：受 SI 限制在 ~56MHz，走不通（doc 23）。

方向 1（BB=1 + 降频）最对症：overflow 由"产生 > 出口"引起，降频直接降产生率。

## 附带修复
- `opencsd_etm4_run.py` 给 `trc_pkt_lister` 加了 RLIMIT_AS 内存帽（libopencsd 1.8.3
  病态分配会涨到 22GB+，已 OOM 过主机）。commit d198793。
