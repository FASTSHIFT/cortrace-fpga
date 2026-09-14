# 提案 27 — LVGL 满速追踪中断重建困境分析

> 日期：2026-07-04
> 状态：分析阶段（待红方排查定位）
> 关联：proposal 25（满速验证）、proposal 26（自适应采样）、r23（红方评审）

---

## 一句话现状

84M 4-bit 满速流式追踪 LVGL，orbetto 能识别 5672 个 PC 点和所有核心 LVGL 函数，但**调用栈每 ~1ms 发生一次"深度失控→安全网 flush"**，SysTick 中断在 47ms 窗口内只被正确检测到 2 次（预期 47 次）。问题高度关联 SysTick 1ms 周期。

---

## 已确认的数据

| 指标 | 值 |
|------|-----|
| TRACECLK | 84 MHz（F429 满速 HCLK/1） |
| 位宽 | 4-bit DDR |
| MMCM PHASE | 117.0°（当前频段最优） |
| unknown% | 2.91%（去帧后 ETM 流中不可识别字节比例） |
| 网络丢包 | 0 |
| FPGA FIFO 溢出 | 0 |
| 总事件数 | 56261（主栈）+ 3（SysTick） |
| max_depth（主栈） | 15（安全网限制 16） |
| unclosed（主栈） | 3 |
| 安全网触发次数 | 40 次 / 47ms = **每 1.175ms 一次** |
| SysTick B 事件 | 2（预期 47） |
| SysTick E 事件 | 1 |

### 活跃中断源（NVIC_ISER 实测）

| ARM 异常号 | IRQ# | 名称 | Handler 地址 | 说明 |
|:---:|:---:|------|:---:|------|
| 15 | — | **SysTick** | 0x08002f4c | 1ms 周期，LVGL tick 驱动 |
| 53 | 37 | **USART1** | 0x0800396c | 串口中断（可能用于调试输出/Touch） |
| 106 | 90 | **DMA2D** | 0x08000ad8 | GPU 加速传输完成中断（LVGL 渲染） |

> 注：DMA2D 中断在 LVGL 渲染刷屏时会高频触发（每帧/每 DMA 传输完成一次）。
> USART1 在串口有数据时触发。
> 这三个中断在 47ms 窗口内的实际触发次数远超仅 SysTick 的 47 次——DMA2D 可能每 ms 触发数次（取决于渲染负载）。
> **总中断频率可能达到 100-200 次/47ms，但 orbetto 只检测到 2 次异常进入。**

---

## 已排除的原因

| 层 | 状态 | 依据 |
|----|------|------|
| 网络传输 | ✅ 无损 | 0 丢包、0 FIFO 溢出、seq 连续 |
| 物理采样（SI） | ⚠️ 2.91% unknown | 117° 是该频段 MMCM 步长内最优；red-team r23 确认残余是 jitter/nibble-slip 本底 |
| TPIU 去帧 | ✅ | `tpiu_deframe_walk` 正常输出，non-monotonic=0 |
| orbetto 解码基础 | ✅ | 5672 PC、56K 事件、函数名全对 |
| branch broadcast | ON | 全量追踪（关 bcast 后数据率降到 KB/s 级不够凑包） |
| 无 SysTick 时 | ✅ 完美 | func_test 禁 SysTick：13/15、0 flushes |

---

## 困境的精确描述

### 症状
安全网每 ~1ms 触发一次 = 精确对应 SysTick 周期。但 SysTick B 事件只有 2 个（47ms 内预期 47 个）。

### 推断的因果链

```
[正常路径]
CPU 执行 LVGL → SysTick 中断 → ETM 发出异常进入包（Branch Address + Exception Info Bytes）
→ orbetto/mortrall 检测到 EV_CH_EX_ENTRY → 切到异常栈 → ISR 执行 → ETM 发退出包
→ orbetto 检测 EV_CH_EX_EXIT → 切回主栈

[实际发生]
CPU 执行 LVGL → SysTick 中断 → ETM 发出异常进入包 → ??? → orbetto 没收到 EV_CH_EX_ENTRY
→ 不切栈 → 但 CPU 确实跳到了 SysTick_Handler → ETM 发了一个 Branch Address 到 ISR 地址
→ orbetto 把它当成普通函数调用压栈 → ISR 返回 → PC 跳回 LVGL 代码
→ 但 LVGL 代码地址和调用栈期望不匹配 → 产生 inconsistent → 栈漂移
→ 累积到 depth=16 → 安全网 flush
```

### 问题缩窄到哪个环节？

**不确定是以下哪一个**（或组合）：

1. **ETM 硬件层**：异常进入包是否真的被正确产生了？（ETM3.5 的异常包是 Branch Address 包的一个变体，包含 1-2 个 Exception Information Bytes，附在地址包末尾。如果 ETM FIFO 内部在中断瞬间有竞争，可能产出不完整的包。）

2. **TPIU 封装层**：异常包被正确产生了但在 TPIU 16-byte frame 封装过程中被截断或错位？（TPIU 的 formatter 在帧边界处如果恰好需要切换 stream-ID，可能把异常包拆到两个帧 → 2.91% unknown 恰好吞掉了帧边界 → 下一帧对不上。）

3. **FPGA 采样层（nibble-slip）**：异常包的字节都到了物理引脚，但 2.91% 的 nibble-slip 恰好命中了 Exception Info Bytes（只有 1 字节！概率分析：每 34 个包有一个字节被吞 → 47 个 SysTick 中有 ~1.4 个的异常字节被吞 → 但实际只检测到 2 个而非 45.6 个——丢失率 95.7%，远超 2.91% 的 random 预期。）

4. **ETM 解码器层（traceDecoder_etm35.c）**：异常包的字节都正确到达了 ETM 解码器输入，但解码状态机在满载数据流中没有正确识别 Exception Info Bytes 的起始位置（需要在 branch-address 包解析时检查是否有额外的异常字节跟随——如果之前有一个 nibble-slip 导致解码器 bit-offset 错了 1 位，后续所有异常字节的识别都会错位）。

5. **mortrall 层**：解码器正确输出了 EV_CH_EX_ENTRY，但 mortrall 的向量表门控 `_isLegitExceptionTarget` 拒绝了它（因为地址不完全匹配——可能异常包里的地址因为 unknown 字节导致部分地址位出错）。

---

## 关键线索：概率不匹配

**2.91% unknown 不能解释 95.7%+ 的异常包丢失。**

### 实际中断频率估算

- SysTick：47 次 / 47ms（确定）
- DMA2D：LVGL 渲染每帧可能多次 DMA 传输，假设 60fps × 2-5 次/帧 = 120-300 次 / 47ms
- USART1：取决于串口流量，假设 0-50 次 / 47ms
- **总计预估：~170-400 次中断 / 47ms**

### 概率分析

- 如果每个 unknown 字节独立随机出现（概率 p=0.0291），一个 2 字节的异常进入包（Exception Info Byte + Branch Address 末字节）被命中的概率 = 1-(1-p)² ≈ 5.7%
- 预期 ~200 个中断中有 200×5.7% ≈ 11 个被吞 → 剩 ~189 个应被检测到
- **实际只检测到 2 个** → 丢失率 **99%**，远超 random 预期

**这说明问题不是"随机 unknown 吞掉了异常包"——而是某个系统性原因导致几乎所有异常包都无法被正确解码。**

可能的系统性原因：
- ETM3.5 异常包解码状态机在"满载流"状态下**一直处于错误对齐状态**（一次 nibble-slip 后后续所有包的字节边界都错了 → 直到下一个 I-sync 重锚才恢复 → I-sync 间隔 1024 字节 → 在 84 MB/s 下只有 ~12μs → 但如果每次 I-sync 后又很快再次 slip...）
- 向量表门控的地址匹配条件太严格（精确到字节 → 如果异常包的地址有 1 bit 错就被拒绝）
- `traceDecoder_etm35.c` 的异常字节解析逻辑有 bug（在满载流中的特定条件下失效）

---

## 需要红方排查的方向

1. **在 `traceDecoder_etm35.c` 的异常包解析路径加计数**：统计"收到 exception info byte"的次数 vs "最终报告 EV_CH_EX_ENTRY"的次数。如果收到了但没报告 → 门控问题；如果没收到 → 解码器对齐问题。

2. **dump 原始 ETM 字节流**中 SysTick_Handler 地址（查 ELF 得到）附近的上下文——手动找 branch-address 包确认异常字节是否存在。

3. **对比 func_test（有 SysTick 但 21M）的成功案例**——functest_nosystick.perf 是 21M 下做的，unknown=0.002%，此时异常检测正常。差异只在 unknown 率。但 2.91% 不应该导致 95.7% 丢失——除非是系统性错位。

4. **看 I-sync 间距**——如果 ETM 解码器在两个 I-sync 之间一直处于错误状态，那 I-sync 间距就是"正确窗口"的大小。统计连续正确解码的窗口长度分布。

---

## 红方提示词

```
你是 ARM CoreSight ETM3.5 解码专家。以下是一个 ETM3.5 满速并口追踪（84MHz 4-bit DDR，branch broadcast ON）的调试问题，需要你定位根因。

背景：
- STM32F429，ETMv3.5 (ETM-M4 r0p1)，TPIU 4-bit 并口 DDR
- FPGA 采样 → UDP 流 → PC 端 orbuculum traceDecoder_etm35.c 解码 → mortrall 重建调用栈
- 去帧后 ETM 字节流的 unknown 比例为 2.91%（物理层 nibble-slip 导致的不可恢复字节）
- 无 SysTick 时（func_test 21M）解码完美，unknown=0.002%，异常检测正常

现象：
- 47ms 时间窗内应有 ~47 次 SysTick 中断（1ms 周期）
- orbetto/mortrall 只检测到 2 次 SysTick 异常进入（EV_CH_EX_ENTRY）
- 丢失率 95.7%，远超 2.91% unknown 的 random 预期（应只丢 ~5.7%）
- 调用栈每 ~1ms 发生一次"深度失控"（因为未检测到的中断进入被当成普通函数调用压栈）

问题：
1. ETM3.5 的异常进入包结构是什么？在 IHI0014Q §7.2 里，Exception Information Bytes 如何附加在 Branch Address 包上？包总长是多少字节？
2. traceDecoder_etm35.c 的解码状态机在什么条件下会"丢失"异常信息？是否存在"一次 nibble-slip 导致后续所有包边界错位"的可能？I-sync 是否能重置对齐？
3. 2.91% unknown 率下，为什么异常进入包的丢失率是 95.7% 而非 5.7%？什么机制会产生这种"放大效应"？
4. orbuculum 的 traceDecoder_etm35.c（具体文件路径：embedded-debug-tools/ext/orbetto/subprojects/orbuculum/Src/traceDecoder_etm35.c）的异常解析逻辑在 line 493-554 附近——它是如何检测 exception bytes 的？是否有已知的满载/高速 trace 下的 corner case？
5. 建议的调试步骤是什么？如何用最少的改动验证问题出在解码器还是物理层？
```
