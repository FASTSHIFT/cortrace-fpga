# 31 - TRACECLK 直采方案落地与低速链路验证

## 背景

H743（Cortex-M7 / ETMv4）的 4-bit 并口 trace 移植中，MMCM 90° 相移采样方
案（proposal 22 §7.1，`trace_capture_mmcm.v`）存在一个硬约束：**它依赖 MMCM
锁定**，而 MMCM 只在 CLKIN ≳10-19MHz 且 VCO=TRACECLK×MULT 落在 600-1440MHz
时才锁，因此**同一个 bitstream 只覆盖一个很窄的 TRACECLK 频段**——H7 固件一
改时钟，采样就失锁。

本轮固件把 CPU 降到 25MHz、TRACECLK 随之降到实测 **12.8MHz**（见下"时钟源"
一节），已经掉出 MMCM 锁定窗口：板测 `trace MMCM lock=0`，尽管 4 条数据线 +
时钟在引脚上连续翻转（`gaps=0`）。MMCM 方案在此频率下**完全抓不到数据**。

## TRACECLK 时钟源（RM0433 Rev8 核实）

> "TRACECLK is the trace port output clock. **It is derived from the PLL1 R
> divider output (pll1_r_ck).**" —— RM0433 §Debug infrastructure

- TRACECLK ← **pll1_r_ck** = vco1_ck ÷ **DIVR1**（`RCC_PLL1DIVR.DIVR1[30:24]`）
- CPU sys_ck ← pll1_p_ck = vco1_ck ÷ DIVP1（或 HSE 直连），是 VCO1 下**另一条
  独立分频支路**
- 两者物理上不同源。本轮实测 CPU=25MHz（HSE 直连，`RCC_CFGR.SWS=HSE`）、
  TRACECLK=12.8MHz，"看起来像主频一半"是分频比的巧合，不是硬件强制关系。
- **DIVR1 只能在 PLL1 禁用时写**（`PLL1ON=0 && PLL1RDY=0`），所以在线用调试器
  改 TRACECLK 会挂死系统，正确做法是在固件/CubeMX 侧改 DIVR1 或 VCO 重编。
- 注：通过调试 AP 读 D3 域 RCC 寄存器（PLLCKSELR/PLL1DIVR 等）会读到复位/陈旧
  值，不可信；以频率计实测 + 手册为准。

## 方案 B（治本）：orbtrace 式 TRACECLK 直采

参考 orbtrace 原版（`orbtrace/orbtrace/trace/glue.py`）的做法：
**把 TRACECLK 直接当 FPGA 采样时钟域**（`ClockSignal().eq(traceclk)`）+
`DDRInput` + `AsyncFIFO` 跨到系统域。无 PLL/MMCM 锁定：

- **频率无关**：TRACECLK 是多少就用多少，5-250MHz（Artix-7 fabric+BUFG 上限）
  内一个 bitstream 通吃，固件改时钟不用重编 FPGA。
- **间歇容忍**：TRACECLK 停/续只是采样时钟域暂停，AsyncFIFO 保住已有数据。
- 代价：用 TRACECLK 自己的边沿采 DDR 数据（而非相移到眼中心）。低速下眼图极
  宽（12.8MHz DDR → 39ns 半位眼），远大于 IDDR setup/hold，无损。相移眼中心
  只在 ~80-100MHz 以上 UI 缩到 ~12ns 时才有必要。

### 实现

- **`orbtrace/syn/artix7/rtl/trace_capture_direct.v`**（新增）：与
  `trace_capture_mmcm.v` **端口完全一致**的直采前端。`IBUF→BUFG` 把 TRACECLK
  变成采样时钟，`IDDR(.C=trace_clk)` 采两沿，`{trace_a[k], trace_b[k-1]}` 打
  包成字节。`mmcm_locked` 输出为伪 lock（复位后跑几个 trace clk 即拉高），让顶
  层 LED/watchdog 把直采前端当作永远健康（它不会失锁）。同步复位（避免
  DRC REQP-1839 RAMB 异步控制告警）。
- **`trace_mmcm_stream_top.v`**：加 `DIRECT` 参数，用 generate 在
  `trace_capture_direct`（DIRECT=1）与 `trace_capture_mmcm`（DIRECT=0）间切换，
  复用整条 AsyncFIFO→packetiser→UDP 路径不变。
- **`run_trace_direct_stream.tcl`**（新增）：DIRECT 构建流。无 capture MMCM，
  采样域即 `trace_clk_in`（IBUF→BUFG），时钟约束只声明 sys MMCM 输出 +
  trace_clk_in + phy，全部异步跨（AsyncFIFO 处理 trace→sys CDC）。

### 板测结果（2026-07-11，TRACECLK=12.8MHz 连续）

综合：0 error，WNS=0.530ns / WHS=0.018ns 全收敛，DRC 仅剩无害 CFGBVS。
烧 QSPI 冷启动后：

- `trace MMCM lock=1`（直采伪 lock），TRACECLK active=1，12.81MHz，4 线翻转
- UDP 流稳定 ~14 MB/s，主机侧加大 rmem 后 **lost_pkts=0**
- orbetto ETMv4 自动识别正确（`post-sync Trace Info 0x01`），**成功恢复调用栈
  （PC bitmap cardinality=1431）并生成 orbetto.perf**

对比 MMCM 方案在同频率 `lock=0` 完全抓不到——直采方案是低速/变频段的治本解。

## overflow 根因二次确认：分支率，不是中断

用户提出中断假设。用调试器实测对比（同一 func_test，12.8MHz，orbetto ETMv4）：

| 配置 | orbetto Overflows | PC cardinality |
|------|-------------------|----------------|
| BB=1，中断开 | 2013 | 1431 |
| BB=1，中断关（PRIMASK=1，已验证保持） | 3317 | 1570 |
| **BB=0，中断开** | **88~102** | 416~769 |

- **关中断不降 overflow（反而略增）** → 中断不是主因。
- **关分支广播（BB=0）overflow 降 95%** → 根因是 branch-broadcast 的字节率超过
  12.8MHz×4bit×2 ≈ 12.8 MB/s 的并口排空带宽（印证 proposal 29：M7 dual-issue
  + I/D cache 的高分支率）。
- BB=0 时 orbetto 靠反汇编 ELF 跟踪直接分支，覆盖 PC 少些但**准确**（栈不被
  overflow 破坏），是低速下的干净配置。

### 固化

`etm_enable_h743.cfg`：TRCCONFIGR 的 BB 位改为 `TRACE_BB` 环境变量控制，
**默认 BB=0**（低速安全）。规则：并口排空带宽 < M7 分支字节率时用 BB=0；
TRACECLK 高到能排空时再开 BB=1 拿全分支锚定。

## 结论与后续

- 低速直采链路**验证通过、无致命错误**（本轮目标达成）。
- 抬高 TRACECLK 非必需：直采频率无关 + BB=0 已近零溢出。若后续要在高
  TRACECLK 下用 BB=1 拿全覆盖，在固件侧调 DIVR1/VCO 重编即可，**FPGA 直采
  bitstream 无需改动**。
- 待办：把直采 bitstream 设为 H7 默认；DIRECT 与 MMCM 两条前端长期共存（高频
  固定用 MMCM 眼中心，低频/变频用 DIRECT）。
