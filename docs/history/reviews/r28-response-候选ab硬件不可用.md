# r28 响应 — 红方候选 (a)(b) 在此 M7 ETM 上硬件不可用（实测）

**日期**：2026-07-19
**回应**：r28 红方方向裁决"若 R1=盲推 → (b) range-filter + (a) 加密 A-sync"
**立场**：R1 已定性为盲推（`r28-response-R1`），本文实测这两个候选的硬件前提，
结论：**两个都在此 STM32H743 的 CoreSight ETM-M7 上不可实现**，必须转纯软件解码侧路线。

---

## 候选 (b)：BB-OFF + address-range filter → **地址比较器未实现，不可用**

ETMv4 的 range-filter 靠 `TRCVIICTLR` + 地址比较器 `TRCACVR`/`TRCACATR`。
本 ETM 实测：

| 寄存器 | 读回 | 含义 |
|------|------|------|
| `TRCIDR4` (+0x1F0) | 0x00114000 | **NUMACPAIRS[3:0] = 0** → 0 对地址比较器 |
| `TRCACVR0` (+0x050) 写 0x08008d40 | **读回 0x00000000** | 写不进，地址比较器物理不存在 |

**NUMACPAIRS=0 坐实：此 M7 ETM 精简实现，无地址比较器。** address-range filter
的硬件基础不存在，红方候选 (b) **不可用**。
（TRCIDR4 其余：NUMPC=4 processing-comparator, NUMSSCC=1 single-shot, NUMRSPAIR=1
resource-selector pair —— 有资源选择器但无地址比较器。）

## 候选 (a)：BB-OFF + 提高 TRCSYNCPR 加密 A-sync → **SYNCPR 固定，不可编程**

A-sync 周期由 `TRCSYNCPR` 控制，但其可编程性由 `TRCIDR3.SYNCPR`[25] 标志：
1=固定不可改。本 ETM 实测：

| 寄存器 | 读回 | 含义 |
|------|------|------|
| `TRCIDR3` (+0x1EC) | 0x07090004 | **SYNCPR[25] = 1** → 同步周期固定 |
| `TRCSYNCPR` (+0x034) 写 0x08 / 0x0d | **均读回 0x0A** | 固定 2^10=1024B，写无效 |

**A-sync 周期硬件固定 1024 字节，不可加密。** 红方候选 (a) **不可用**。

---

## 剩余可行路线：纯软件解码侧盲推约束（唯一出路）

两个片上"缩盲推跨度"手段（range-filter 削区间、加密 A-sync 补锚点）硬件都不可用。
结合 R1 已证的事实（**采集干净、13/13 地址锚点全落合法 .text，走偏纯是盲推越过函数边界
进物理相邻函数**），唯一不依赖 ETM 硬件特性的补偿是**解码器侧约束盲推**：

- **ELF 合法目标集约束**：BB-OFF 盲推时，解码器顺 ELF 反汇编推断直接分支流。假调用的根源
  是盲推在稀疏锚点间**越过函数边界**（core_list_init@0x7f54 盲推进物理相邻 HAL_UART_Init
  @0x791c）。可在解码侧加约束：盲推遇到函数边界（ELF 符号表边界）时，若无地址锚点确认
  跨界，则**不生成跨函数 transition**（等下一个间接分支锚点再定位），把"顺 ELF 硬推越界"
  改成"边界处保守停顿"。
- 这是 opencsd/orbetto 解码器层的改动，**零 ETM 硬件依赖、零上板**，且 R1 已证采集数据
  质量足够支撑（地址锚点全干净）。

**下一步**：评估 opencsd_etm4_run / orbetto Mortrall 的盲推实现，看能否加"函数边界处无锚点
不跨界"约束；或退一步——**接受 BB-OFF 调用图在 cache 稀疏锚点下的固有盲推误差，改用采样式
统计**（多次 one-shot 抓不同片段，热点函数出现频次统计，而非精确逐 transition 调用图），
这与 R2 裁决的"采样式剖析"定位一致。
