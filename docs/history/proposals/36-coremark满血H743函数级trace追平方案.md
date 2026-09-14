# Proposal 36：满血 H743（480MHz）跑 CoreMark，函数级 trace 采样剖析实测方案

> 日期：2026-07-19
> 状态：阶段 0-3 + R1/R2 实测完成，目标已按红方 r28 收缩（见下方修订说明）
> 目标（**r28 修订后，诚实版**）：让 STM32H743 在**满血 480MHz**、`-O3`、cache 全速跑
>       CoreMark 时，用 ETM 过滤（BB-OFF + range-filter）把 trace 字节率削峰到
>       **TRACECLK=112.5MHz** 4-bit DDR 端口（≈112.5 MB/s）能承载的范围，对**选定热点函数**
>       做**采样式/区间过滤**的函数级调用时间线重建。
>
> **⚠️ r28 目标收缩说明（R2/R3 实测后）**：原目标"全程无损追平满血 480M 全函数进出"
> **经 R2 实测证伪、正式放弃**。实测（`reviews/r28-response-R2生成率真值.md`）：BB=1 生成率
> 外推 G₄₈₀ ≈ 97~172 MB/s，且**溢出是峰值簇驱动**（150M 均值仅 33MB/s 却因峰值簇溢出 183 次，
> 峰均比 >3.4×），480M 峰值簇必然灌爆 4KB ETF + 112.5M 端口；且此 H7 拓扑下 TRCSTALL 停核
> 无效（ETF 溢出丢帧不回压）。故可达目标 = **"满血负载下热点函数采样式/range-filtered 剖析"**，
> **不是"全程无损追平"**。
> 关联：
> - proposal 29（M7 trace 溢出根因：生成率 vs drain 率失衡，本方案的理论基础）
> - proposal 33（per-lane IDELAY）、doc 16（clock-lane IDELAY 突破 100M→112.5M）
> - proposal 26（频率自适应采样）、proposal 25（满速流式带宽与解码完整性）

---

## 0. 一句话目标

**满血 M7（480MHz + I/D cache + 双发射 + `-O3`）跑 CoreMark 会把全指令 trace 灌爆任何
出口；本方案用 ETMv4 的过滤能力只保留"函数进出"事件，把字节率降一个数量级，让
112.5MHz 端口连续无损接住，从而在不停核、不降主频的前提下重建 CoreMark 的真实函数调用
时间线，验证这套 A7-Lite 采集链能"追上满血 H743"。**

---

## 1. 为什么这件事有意义（也有挑战）

### 1.1 满血 M7 的全指令 trace 不可能被 112.5M 端口接住

proposal 29 已坐实：M7（cache + 双发射 + 零等待）跑密集分支代码，branch broadcast 下
trace 字节率暴涨；当时 50M 端口在 12.8MHz CPU 的 func_test 上就 1356 次溢出。现在要
**480MHz + CoreMark + -O3**，全指令 + 全分支广播的字节率是天文数字，端口再快也接不住。

**结论：必须降生成率，不能只靠提 drain（proposal 29 方案 A 已到 112.5M 的采集眼极限）。**

### 1.2 "函数进出"是信息量与带宽的最佳折中

用户的洞察正确：大多数性能剖析/调用时间线只需要**函数边界事件**（谁调用了谁、何时进、
何时出），不需要每条算术指令。ETM 层面，函数进出对应：
- **BL / BLX**（调用）→ 目标地址
- **BX LR / POP {…,PC}**（返回）→ 返回地址（间接分支）
- 异常进入/退出（ISR 边界）

如果只保留这些，字节率 ≈ **调用/返回速率 × 每事件字节数**，比全指令 trace 低 1-2 个数量级。

### 1.3 挑战点（必须诚实面对）

1. **ETMv4 没有"只发 BL/BLX/POP"的单一开关**。要用组合手段逼近（见 §3）。
2. CoreMark 的热点（`core_list_find`、`matrix`、`crc`、`state machine`）**有深循环**，
   循环体内分支多但都是同一批地址——需确认过滤后字节率真的降下来。
3. 112.5M 端口 + 60KB BRAM 当前是 **one-shot 快照（547µs 窗口）**；CoreMark 一轮
   ~数十 ms，单快照只能采样一个片段。要么接受采样式，要么上流式/大缓冲（§5）。
4. "追平满血 H743" 的判据要定义清楚：是 **CoreMark 分数**（CPU 侧，与 trace 无关）还是
   **trace 重建的调用时间线正确性 + 覆盖率**（采集侧）——本方案聚焦后者，前者作为 CPU
   健康度的旁证。

---

## 2. 系统架构（复用已固化的 112.5M 链路）

```
STM32H743 @480MHz, -O3 CoreMark
  │  ETM-M7 (ETMv4): ViewInst=trace-all, BB 受控, cycle-count 关, TS 受控
  ▼
ETF (4KB) → TPIU 4-bit @ TRACECLK=112.5MHz (pll1_r_ck/2 DDR)
  │  (drain: 112.5 MB/s raw 上限)
  ▼
A7-Lite FPGA: trace_capture_a7 (IDDR + data-IDELAY + clock-IDELAY, doc16 突破件)
  │  {trace_b,trace_a}/周期 → 60KB BRAM one-shot（或流式，§5）
  ▼
UDP :5001 读出 → host
  ▼
orbetto (官方 TPIU deframe → ETMv4 Mortrall) → Perfetto 调用时间线
```

**关键复用**：doc 16 的 clock-lane IDELAY 件（`trace_iddr_clktap.bit`）已实测 112.5M 真实
TRACECLK 零字节错、14/14、顺序 PASS。本方案不动采集前端，只改**ETM 配置（降流量）**和
**firmware（CoreMark + 480M + O3）**。

---

## 3. 核心技术：如何让 ETMv4 只发"函数进出"

按对带宽的压缩力度 + 实现难度排序，分三档，**建议逐档验证**：

### 档位 A（首选，最简）：BB OFF + ViewInst 全程 + 关 cycle-count

- **BB OFF**（`TRCCONFIGR.BB=0`）：直接分支（含大部分 BL）**不再发地址包**，解码器靠
  ELF 反汇编静态推断直接分支流。**只有间接分支（BLX 寄存器、BX LR、POP PC）和异常**
  才发地址。这天然就是"函数进出的锚点"——调用返回是间接分支，必发地址。
- **关 cycle-count**（`TRCCONFIGR.CCI=0`）、**关全局时间戳**（`TS=0`）：省掉周期性
  cyccnt/timestamp 字节（我们用 FPGA 硬件时间戳，doc 16，不需要 ETM 内时间）。
- **效果**：字节率 ≈ 间接分支率 + 周期性 A-sync。proposal 29 估计 BB OFF 可降一个数量级。
- **代价**：直接分支靠 ELF 推断——**要求解码器有完整 ELF 且指令流连续**。CoreMark 的
  间接调用（函数指针、`core_bench_list` 的比较回调）和所有返回仍精确。**这正是"函数进出
  级"trace 的自然形态。**
- **判据**：解出 CoreMark 顶层函数调用序（`core_bench_list/matrix/state`、`crcu*`…）+
  Perfetto 时间线，覆盖率与静态调用图对齐。

### 档位 B（若 A 仍超带宽）：ViewInst 地址范围过滤，只 trace 关心的函数

- `TRCVIICTLR` + `TRCACVR`/`TRCACATR`（address comparators）设 include-range，只对
  CoreMark 的**顶层函数入口区间**开 trace，把叶子算术函数（`crcu8`、内联 kernel）排除。
- ETMv4 有 4-8 对地址比较器（实现相关，需读 TRCIDR），可框选几个热点区间。
- **代价**：只看框选区间的调用；配置更复杂，要先从 ELF 算出各热点函数地址范围。

### 档位 C（终极，若要极致压缩）：只留间接分支 + 抑制直接分支推断开销

- 结合档 A（BB OFF）+ 档 B（range filter）+ 提高 `TRCSYNCPR` 同步周期（减少 A-sync
  开销，但过疏会掉锚点，需权衡）。
- 这档把 trace 压到近乎"纯 call/return 事件流"，是带宽最省的极限。

**推荐路径：先档 A（改一个寄存器位），量字节率；不够再叠档 B。**

---

## 4. 带宽预算（必须先算，避免白跑）

### 4.1 端口承载上限（已知）

- TRACECLK=112.5MHz，4-bit DDR → raw 字节率 = 112.5 MB/s（每 TRACECLK 周期 1 字节
  `{trace_b,trace_a}`）。这是 **drain 硬上限**。
- 但 raw 里含大量 TPIU HSYNC 填充（trace 稀疏时占比高）；真正的 ETM 净荷率 = 生成率。
- **只要 ETM 生成率 < 112.5 MB/s 且不出现持续峰值超过 ETF(4KB) 缓冲，就无损。**

### 4.2 满血 M7 CoreMark 的 ETM 生成率（需实测，先给量级估算）

- CoreMark @480MHz ≈ 480 × (CoreMark/MHz，M7 典型 ~5.0) ≈ 2400 CoreMark。
- 指令吞吐峰值 ~2 指令/周期 × 480M = 960 M 指令/s。
- 分支密度（CoreMark 混合负载）粗估 ~10-15%，即 ~100-140 M 分支/s。
- **全 BB（每分支发地址）**：假设每分支均摊 ~1.5 字节 → ~150-210 MB/s。**超 112.5，溢出。**
- **BB OFF（只间接分支/返回发地址）**：间接分支占比远低（多数是直接 BL + 条件分支），
  粗估 ~10-30% 的分支是间接/返回 → ~15-60 M 事件/s × ~4 字节（长地址）≈ 60-240 MB/s
  上界估计……**这里不确定性大，必须实测**（§6 步骤 2）。
- 若档 A 不够，档 B 的 range filter 能把生成率再砍到只剩顶层函数调用。

> ⚠️ **诚实标注**：以上是数量级估算，真实分支/间接分支占比取决于 CoreMark 编译结果与
> M7 分支预测行为，**必须用步骤 2 的实测字节率替换这些估算**，不能当结论。

### 4.3 采集窗口（one-shot 60KB vs 流式）

- 60KB BRAM @ 净荷率 X MB/s → 窗口 = 60KB/X。若 X=50MB/s → 1.2ms；若 X=112MB/s →
  0.55ms。CoreMark 一轮数十 ms，**单快照只覆盖一个片段**。
- **对策**：(a) 采样式——多次 one-shot 抓不同片段，统计覆盖（够验证调用行为）；
  (b) 流式——FPGA 侧 deframe 去 HSYNC 后千兆流出（raw 112MB/s 撞千兆墙，去 HSYNC 后可行），
  这是 proposal 25 方向的延伸，工程量大，列为可选。

---

## 5. 拆解：单变量递增的推进步骤（每步只改一个东西）

**核心纪律（用户定，与本项目一路的方法论一致）**：每个阶段**只引入一个新变量**，上一步
绿了才动下一步。任何一步崩/误码飙升，立刻定位到那唯一的新变量，不叠加干扰。全程复用
已固化的 112.5M clktap 采集前端 + 当前解码链，不动它们。

基线锚点：当前 func_test @ CPU 150M / TRACECLK 112.5M / -O0 / cache 未显式开 / BB=1
已实测 698 PC、14/14、字节错 0.000%、顺序 PASS（doc 16）。以此为已知良好起点。

---

### 阶段 0：只换 workload —— CoreMark 移植，其它全不动
- **唯一变量**：把 func_test 换成 CoreMark 源码。**保持当前一切**：CPU 150M、TRACECLK
  112.5M、`-O0`、cache 现状、BB=1、ETM 全配置照旧。
- 移植官方 CoreMark 到 `H743_CoreMark`（最小外设依赖，串口打印分数；`ITERATIONS` 调小
  到几百 ms 一轮便于抓取）。
- **产出**：CoreMark 分数（-O0 会很低，无所谓，只验证能跑）；DWT 实测 sysclk=150M 不变。
- **判据**：① CoreMark 结果自检通过（`[0]crclist` 等校验值正确）；② ETM 抓样能解出
  CoreMark 顶层函数（`iterate`→`core_bench_list/matrix/state`），字节错仍 ~0。
- **若崩**：问题一定在 CoreMark 移植本身（栈/堆/链接），与频率/优化/过滤无关。

### 阶段 1：只开 -O3 —— 频率/cache/过滤都不动
- **唯一变量**：`-O0` → `-O3`（`-funroll-loops` 可选）。CPU 仍 150M，TRACECLK 仍 112.5M，
  BB 仍 =1。
- -O3 会内联 + 优化，trace 变化：分支密度改变、字节率上升。
- **产出**：-O3 下的 CoreMark 分数（应大幅高于 -O0）、ETM 字节率、Overflow 计数。
- **判据**：能否仍解出调用图（-O3 内联后函数变少但顶层调用仍在）；记录字节率作为
  "是否需要过滤"的依据。
- **若字节率/溢出开始超标**：说明 -O3 + BB=1 已逼近端口，正好引出阶段 2 的过滤需求。

### 阶段 2：只开过滤（BB OFF）—— 频率/优化不动
- **唯一变量**：`TRCCONFIGR.BB` 1→0（顺带关 CCI/TS，因为我们用 FPGA 时间戳）。
  只 trace 函数进出（间接分支 BLX/BX LR/POP PC + 异常）。CPU 仍 150M，-O3 保持。
- **产出**：BB OFF 后的字节率（对照阶段 1 的 BB=1），Overflow。
- **判据**：① 字节率显著下降（目标降一个数量级）；② 解码器靠 ELF 推断直接分支，仍解出
  正确的函数进出时间线；③ 采集字节错 <1%。
- **若 BB OFF 仍超带宽**：触发档 B（`TRCVIICTLR` + 地址比较器 range filter，只框顶层
  热点函数）——这是阶段 2 内的子步骤 2b，仍只在"过滤"这个变量域内细化。

### 阶段 3：只开 I/D cache —— 频率/优化/过滤不动
- **唯一变量**：使能 I-cache + D-cache（`SCB_EnableICache/DCache`）。CPU 仍 150M，-O3 +
  BB OFF 保持。
- proposal 29 已证：cache 是 M7 trace 流量暴涨的直接推手（关 cache 溢出 810→0）。这一步
  单独验证"cache 开 + 已过滤"的字节率是否仍在端口承载内。
- **产出**：cache 开后的字节率跳变、Overflow、CoreMark 分数跃升（cache 对分数贡献巨大）。
- **判据**：过滤后即使 cache 开，字节率仍 < 112.5MB/s 且低错；若超，回到阶段 2 加 range
  filter 或缩小 trace 区间。

### 阶段 4：逐步提 CPU 频率 —— 每次只提一档
- **唯一变量（每次）**：CPU 频率 150M → 200M → 300M → 400M → **480M（满血）**，一档一验。
  **TRACECLK 尽量锁定 112.5M 不变**（重算 PLL：sysclk 由 DIVP1、pll1_r_ck 由 DIVR1 独立
  分 VCO；480M sysclk + 225M pll1_r_ck 若约束冲突，允许 TRACECLK 落 100-112M，clktap 件
  覆盖）。每档 DWT 硬测频率 + halt 下核对 PLL（防超频崩，重演 8-vs-25MHz 教训）。
- **产出**：每档的 CoreMark 分数曲线、ETM 字节率曲线、Overflow、采集字节错。
- **判据**：每提一档，字节率随之升；只要仍无损/低错就继续；直到某档字节率触端口上限
  或抓取窗口不足 → 进阶段 5。

### 阶段 5（条件触发）：窗口不够 → 上实时流式
- **触发条件**：高频 + 满血下，60KB BRAM one-shot 窗口（~0.5-1.2ms）不足以覆盖 CoreMark
  的完整行为，或需要连续观测。
- **唯一变量**：采集架构 one-shot BRAM → **连续流式**。
  **复用已验证资产**：proposal 25 的 `trace_mmcm_stream_top`（F429 84M 实测端到端
  83.6MB/s、0 丢包、心跳包 + ARP deadlock breaker 已解决）。移植其流式 packetiser 到当前
  IDDR + clktap 采集前端（把 BRAM one-shot 换成 FIFO→千兆 UDP 连续流）。
- **带宽墙**：raw 112.5MB/s 逼近千兆有效 ~117MB/s（余量 4%）。**关键：FPGA 侧先做
  TPIU deframe/去 HSYNC 只流真 ETM 净荷**（BB OFF + 过滤后净荷远低于 raw），才留出余量。
- **产出**：连续流式端到端，长窗口 CoreMark 调用时间线。
- **判据**：0 丢包、字节错 <1%、能连续采到完整 CoreMark 轮次。
- **回退**：流式复杂，若受阻回退到阶段 4 的 one-shot 采样式（多次快照统计覆盖），仍能
  验证满血负载下的函数级 trace 正确性。

### 阶段 6："追平满血 H743"的对齐论证
- **CPU 侧**：480M CoreMark 分数 ≈ ARM 公布的 M7@480M 参考（~2000-2400，旁证 CPU 满血）。
- **采集侧**：满血 480M + CoreMark + cache + -O3 负载下，函数级 trace 无损/低错重建调用
  时间线 + Perfetto（`coremark_480m_112m.perf`）。
- **诚实边界**：不声称"全指令无损追平"（那需 >112M 端口或停核）；声称
  **"函数进出级、满血 480M 负载、连续或采样、低错"**——112.5M 端口 + 函数级过滤的合理
  能力定位。

---

### 实测进度

**阶段 0 完成（2026-07-19）✅** —— 只换 workload 为 CoreMark，150M/-O0/BB=1/其它不动：
- CoreMark 计算正确（三算法 CRC 全匹配官方值：crclist 0xe714 / crcmatrix 0x1fd7 /
  crcstate 0x8e3a）、486 Iter/s @150M -O0、UART(PA9/PA10) 完整报告。
- **ETM trace 解出 CoreMark 核心函数**：112.5M TRACECLK 抓样 → 614 PC / 100% 落 flash /
  解出 11 个 CoreMark 函数（core_bench_matrix、core_bench_state、core_list_mergesort、
  core_state_transition、crc16/crcu8/16/32、matrix_add_const/test、calc_func）。
- **采集字节错 0.000%**（RESERVED=0, BAD_SEQ=0，3349 指令区间全合法）。
- 工程要点：board_clock_override 需先关 PLL 再重配（H7 运行中 PLL 不可改）；CoreMark 改
  无限循环让 ETM 流持续（单次 -O0 跑完即 idle 无 trace）；UART 在 clock override 后 re-init;
  跨会话 IDDR 半-nibble 相位偏移需断电复位。

**阶段 1 完成（2026-07-19）✅** —— 只改 -O0→-O3，频率/过滤/cache 不动：
- CoreMark CRC 仍全对（计算正确）。**反常：-O3 更慢**（228 Iter/s vs -O0 486，Total 8.7s
  vs 4.1s）。DWT 硬测 CPU=150.1M 确认时钟正常。根因=**无 cache + flash 等待**：HCLK=75M、
  I/D cache 未开、CoreMark 指针追逐密集（list/matrix/state），-O3 的激进内联/循环展开假设
  有 cache，无 cache 时更多 flash stall 反被拖慢。**预演了阶段 3（开 cache）的必要性**
  （doc 29 早证 M7 靠 cache 全速）。非 bug。
- **ETM trace：999 PC / 100% flash / 采集字节错 0.000%**（RESERVED=0，9765 指令区间全合法）。
- **trace 更密**：fsync 3418(-O0)→18(-O3)，deframed 7660→16313 —— -O3 分支/数据更密集，
  HSYNC 填充锐减，trace 字节率上升但仍在端口承载内、零字节错、能解出调用图（core_bench_list/
  state、core_state_transition、crc16/crcu16/crcu32）。

**阶段 2 完成（2026-07-19）✅** —— 只改 BB=1→BB OFF（TRCCONFIGR.BB=0），-O3/150M/cache 不动：
这是方案核心"只 trace 函数进出"。BB OFF 后直接分支不发地址（解码器靠 ELF 推断），只有
间接分支/返回/异常发地址 = 函数进出锚点。

| 指标（同 -O3 CoreMark）| BB=1（阶段1）| **BB OFF（阶段2）** |
|---|---|---|
| deframed ETM 字节 | 16313 | **2760** |
| non-HSYNC 数据占比 | ~26% | **5.1%** |
| 实际 trace 字节率 | 高 | **降 ~5-6×** |
| unique PC | 999 | 656（100% flash）|
| 采集字节错 | 0.000% | **0.000%** |
| 调用图 | 完整 | **完整**（core_bench_state⇄state_transition, crcu16→crcu32）|

- **BB OFF 把 trace 字节率降一个数量级**（deframed 16313→2760，~6×），正是方案核心：全指令
  trace 压成函数进出流。这对追平满血 480M 关键（全 BB 会灌爆端口）。
- 调用图仍完整正确（BB OFF 下靠 ELF 静态推断直接分支，间接分支/返回重建函数进出），采集
  字节错 0.000%。产物 `coremark_o3_bboff_112m.perf`。

**阶段 3 完成（2026-07-19）✅** —— 只加开 I/D cache（`-DBOARD_ENABLE_CACHE`，运行时
SCB_EnableICache/DCache），-O3/BB-OFF/150M 不动：
- **CoreMark 分数跃升**：228 Iter/s（阶段1 -O3 无cache）→ **612 Iter/s**（-O3+cache），
  提升 **2.67×**，修复了阶段1的"-O3 反慢"，坐实 doc 29"M7 靠 cache 全速"。CRC 仍全对。
- **cache 全速 + BB-OFF 过滤，trace 字节率仍可控**：non-HSYNC 5.1%(无cache BB-OFF)→
  **6.7%**(cache+BB-OFF)，仅微升，**远低于端口承载**。1008 PC / 100% flash /
  **采集字节错 0.000%**（opencsd_etm4_run，1109 指令区间 RESERVED=0）。
- **对照 doc 29 的决定性意义**：当年 cache+全BB 溢出 1356 次；现在 cache+BB-OFF 过滤
  **0 溢出、0 字节错**——**过滤是 cache 全速下不溢出的关键**，验证了方案主线。
- 注：orbetto 的 TPIUPump 对这个极稀疏(6.7%数据/高fsync)BB-OFF 流导出 Perfetto 时
  cardinality=0（opencsd 官方 deframer 正常出 1008 PC）——是 orbetto deframe 对稀疏流的
  适配问题，非采集问题，后续单独修（阶段验收用 opencsd 逐指令判据）。

**阶段 3 补充：cache 揭出两个硬约束（2026-07-19）⚠️**

阶段 3 开 cache 后，对 Perfetto 调用栈做了逐函数 vs ELF objdump 的对齐核对（不是集合覆盖），
发现两个之前乐观结论没暴露的硬约束：

**约束 1：BB-OFF + cache → 调用图走偏（解码推断误差，非采集错）**
- 逐 transition 对照 ELF 真实 BL 目标：cache+BB-OFF 出现大量**假调用**——
  `core_list_init→HAL_UART_Init ×22`（core_list_init 在 ELF 里根本不调用任何函数）、
  `matrix_test→core_init_matrix ×3`（matrix_test 只调 crc16）。
- **决定性对照**（三配置同 workload）：
  | 配置 | 假调用 | 采集字节错 |
  |------|:---:|:---:|
  | BB-OFF 无 cache（阶段2）| **无**（604 visit，17 transition 全合法）| 0.000% |
  | **BB-OFF + cache（阶段3）** | **多**（HAL_UART_Init×22 等）| 0.000% |
  | BB=1 + cache | 无（仅真实 SysTick→HAL_IncTick）| 0.019% |
- **机制**：BB-OFF 用"带宽换推断"——不发直接分支地址，解码器靠 ELF 反汇编**盲推**两个间接
  分支锚点之间的直接分支流。cache 命中让 M7 双发射零等待全速跑，间接分支**锚点物理间距拉大**
  （visit 190 vs 无cache 604），盲推路程超出可靠范围，一个数据相关条件分支猜错方向就顺着
  ELF **走进物理相邻的错误函数**（core_list_init@0x7f54 紧邻 HAL_UART_Init@0x791c），无锚点纠回。
- **本质**：字节错仍 0（采集没问题），是 **BB-OFF 推断模型在 cache 稀疏锚点下的解码局限**。
  BB=1（每分支发地址、不盲推）在 cache 下调用图干净 → 坐实是"BB-OFF 盲推 × cache 稀疏锚点"
  的组合，非 cache 本身或采集。

**约束 2：BB=1 + cache 在当前 150M 就已爆带宽**
- 实测 BB=1+cache @150M CPU / 112.5M TRACECLK：**ETF 溢出 37-64 次**——ETM 生成率已 >112.5MB/s
  端口 drain 上限。抓样有效数据占比 ~50%（端口限制后的值，真实生成率更高）。
- 即 **cache 全速 + 全 BB，150M 主频就溢出**（与 doc 29 一致：cache+全BB 在 12.8M 就溢出）。
  480M 满血更是远超。**BB=1 不是满血 480M 的可行选项**。

**两难**：BB=1 准但爆带宽（150M 即溢出）；BB-OFF 省带宽但 cache 稀疏锚点下调用图走偏。
需要红方评审下一步方向（见评审请求）。候选：(a) BB-OFF + 提高 TRCSYNCPR 加密周期性 A-sync
锚点补偿盲推；(b) BB-OFF + address-range filter 只 trace 关心区间（缩短盲推跨度）；
(c) 降 CPU 主频找 BB=1 不溢出的临界点（但离"满血"远）；(d) TRCSTALL 停核无损（侵入式）。

**红方 r28 评审 + R1/R2 回应（2026-07-19）** —— 见 `reviews/r28-*.md`：

**R1（走偏=盲推 还是 采集丢锚点？）→ 盲推**（`r28-response-R1区分实验.md`）：
- 固件==ELF 核对：cmp 仅差 12 字节段间隙填充（0x00 vs 0xFF），代码全一致。
- BB-OFF+cache 流 ~3200 atom 仅 13 个真实地址锚点，**13/13 全落合法 .text 指令区**（无脏地址）。
- 黄金对照：BB=1+cache+75M（不溢出）ground truth 调用图**干净**（945 visit，0 假调用），
  BB-OFF+cache 有 HAL_UART_Init×22。两配置采集前端相同 → **走偏纯粹是 BB-OFF 盲推模型在
  cache 稀疏锚点下的解码局限，非采集丢字节**。红方"采集丢锚点"证伪。
- 教训：选 tap 用 RESERVED 字节错判据，不用 A-sync 数（A-sync 多的点字节错反而高）。

**R2（真实无钳制生成率）→ 已测，480M 不可达确认**（`r28-response-R2生成率真值.md`）：
- **TRCSTALL 停核法在此 H7 拓扑不成立**：150M BB=1 加 TRCSTALLCTLR=0x10C，CoreMark 分数
  611.89 一点不降（CPU 从未被节流）——ETF 溢出丢帧不回压 ETM FIFO，停核条件永不满足。
  **红方候选 (d) 同时报废**。
- 改用频率外推法，两个 0-溢出真值点：**G(50M)=17.9 MB/s、G(75M)=22.5 MB/s**
  （净 deframed / 546µs 真实窗口；112.5M/150M 已溢出，净字节是封顶残值不算）。
- 外推 **G₄₈₀(BB=1) ∈ [97,172] MB/s**（三种模型），全部逼近或超 112.5 端口。
- **关键发现（比红方 R3 更狠）**：G₁₅₀ 均值仅 ~33 MB/s（远低于端口），**却溢出 183 次**
  → **溢出是峰值簇驱动，峰均比 >3.4×**。480M 峰值簇（×3.2）必然灌爆 4KB ETF。
  **"全程无损追平满血"物理不可达，且瓶颈是峰值簇非均值带宽**（正面坐实红方 R3 第 5 点）。

**方向裁决（R1+R2+R3 合并）**：
1. 正式放弃"全程无损追平满血 480M"（§0 目标已收缩）。
2. **BB-OFF 削峰是唯一片上降峰值手段**；配 range-filter 缩盲推跨度（R1 证盲推可控）。
3. 下一步：落地 **BB-OFF + TRCVIICTLR/TRCACVR address-range filter**（红方候选 b），
   只 trace 热点函数区间，缩短盲推跨度验证 cache 全速下调用图不再走偏。

**候选 (a)(b) 硬件可行性实测（2026-07-19）→ 均不可用**（`r28-response-候选ab硬件不可用.md`）：
- **候选 (b) range-filter 不可用**：`TRCIDR4.NUMACPAIRS=0`（0 对地址比较器），写 `TRCACVR0`
  读回 0 —— 此 M7 ETM 精简实现，**无地址比较器**，硬件 range-filter 基础不存在。
- **候选 (a) 加密 A-sync 不可用**：`TRCIDR3.SYNCPR[25]=1`（同步周期固定），写 `TRCSYNCPR`
  0x08/0x0d 均读回 0x0A —— A-sync 周期**硬件固定 1024B，不可编程**。
- **两个片上"缩盲推跨度"手段硬件都不可用。** 唯一剩余路线 = **纯软件解码侧盲推约束**
  （ELF 函数边界处无锚点不跨界，抑制假调用，零上板）或退一步接受 **BB-OFF 采样式统计**
  （多次抓样统计热点函数出现频次，而非精确逐 transition 调用图）——与 R2 裁决"采样式剖析"
  定位一致。这是下一步落地方向。

### 阶段依赖图（单变量链）

```
阶段0 换CoreMark(其它不动)
  └─绿→ 阶段1 只加-O3
          └─绿→ 阶段2 只加BB-OFF过滤 (不够→2b range filter)
                  └─绿→ 阶段3 只开I/D cache
                          └─绿→ 阶段4 逐档提频 150→...→480M
                                  └─窗口不足→ 阶段5 上流式(复用proposal25)
                                          └─→ 阶段6 追平论证
```

每个 └─绿→ 都是"上一步字节错~0 + 调用图正确"才推进；任一步红，变量唯一，立即定位。

---

## 6. 风险与回退

| 风险 | 影响 | 缓解/回退 |
|------|------|-----------|
| 480M + 225M pll1_r_ck 的 PLL 约束无解 | TRACECLK 偏离 112.5M | 允许 TRACECLK 落 100-112M，用 doc16 clktap 件覆盖；或牺牲少量 sysclk |
| 档 A(BB OFF) 字节率仍超端口 | 溢出 | 叠档 B range filter；或采样式短窗口 |
| BB OFF 下直接分支推断需连续流 | 稀疏段掉锚点 | 保持合理 TRCSYNCPR；range filter 保证热点区连续 |
| one-shot 窗口只覆盖片段 | 看不到完整一轮 | 阶段5 上流式（**复用 proposal 25 已实测的 F429 84M 流式：83.6MB/s、0 丢包、心跳+ARP breaker**）；或多次采样统计 |
| 480M @ VOS0 稳定性/flash WS | CPU 崩(重演超频教训) | 阶段4 逐档提频，每档 DWT 硬测 + halt 核对 PLL；CubeMX 生成配置 |
| CoreMark 移植工作量 | 拖进度 | 先用现成 M7 CoreMark 移植参考；最小化外设依赖 |
| 多变量同时改导致定位难 | 出错找不到根因 | **单变量递增纪律（§5）**：每步只动一个变量，上步绿才下一步 |

**全程可回退**：所有改动在 git；采集前端不动（复用已固化 112.5M clktap 件），失败只影响
firmware/ETM 配置，`git checkout` + 烧回 tclk106 即恢复当前最佳态。**单变量链（§5 依赖图）
保证任一步崩溃时，新变量唯一、可立即定位并回退到上一绿态。**

### 已验证可复用资产（降低本方案风险）

| 资产 | 来源 | 状态 |
|------|------|------|
| 112.5M clktap 采集前端 | doc 16 | ✅ 698 PC / 14-14 / 字节错 0 / 顺序 PASS |
| ETMv4 解码 + Perfetto + FPGA 时间戳 | doc 16 | ✅ 端到端跑通，调用频次逐函数校验 |
| orbetto ETMv4-CMSIS 异常退出修复 | embedded-debug-tools | ✅ SysTick entry/exit 配平 |
| **流式 packetiser（千兆 UDP，0 丢包）** | **proposal 25** | ✅ **F429 84M 实测 83.6MB/s，阶段5 移植基础** |
| M7 trace 溢出机制 + 各降流量手段 | proposal 29 | ✅ 理论基础，BB/cache/range 已量化 |

---

## 7. 交付物

1. `H743_CoreMark` firmware（480M, -O3），CoreMark 分数打印。
2. ETM 过滤配置：`target/etm_coremark_h743.cfg`（档 A/B 可选，env 切换）。
3. 各阶段实测数据表：全 BB 溢出基线、档 A/B 字节率、采集字节错率、解码覆盖。
4. `coremark_112m.perf`（Perfetto 函数调用时间线）+ 交叉校验报告。
5. 文档更新：把"满血 480M 函数级 trace 追平"结论写入 doc 16 或本 proposal 收尾。

---

## 8. 给红方的预设质疑点（自检）

1. **"函数进出"的 ETM 语义是否精确？** BB OFF 下直接分支靠 ELF 推断，若 CoreMark 有
   计算型间接跳转（switch 表）会不会漏/错？→ 需在阶段 2 用已知调用图核对。
2. **带宽估算的不确定性**：§4.2 的 60-240MB/s 上界跨度太大，结论必须靠阶段 2 实测，不能
   拿估算当验收。
3. **"追平满血"是否偷换概念**？本方案明确只声称"函数级、满血负载、低错"，不声称全指令
   无损——§5 阶段 4 已划清边界。
4. **one-shot 采样能否代表连续行为**？采样式统计 vs 真流式的代表性，需在阶段 3 说明。
5. **480M/VOS0 会不会重演超频崩溃**（8 vs 25MHz HSE 教训）？→ 阶段 0 用 DWT 硬测 + halt
   下核对 PLL，先证 CPU 稳。

---

## 【重大更正 2026-07-20】阶段3假调用根因推翻 + 阶段4冲频完成

**阶段3 的"BB-OFF+cache 假调用"结论已被 proposal 38 推翻**：真凶不是 cache 稀疏锚点盲推，
而是 `make_opencsd_snapshot.py` 的 **mem.bin 基址 bug**（objcopy 剥掉 .isr_vector 后 binary
从 .text LMA 0x080002a0 起，但硬编码基址 0x08000000，全局错位 0x2a0 字节）。BB=1 每分支发
绝对地址掩盖了错位，BB-OFF 靠镜像盲推暴露了它——故"BB=1 干净/BB-OFF 脏"是 bug 假象，非 ETM
机制。修复后 BB-OFF+cache 调用图干净、1219/1219 调用边对 ELF。详见
`proposals/38-假调用真凶-快照mem基址bug-推翻r28r29.md`。

**因此本方案 §5 阶段4-6 的悲观前提（"cache 下 BB-OFF 必漂移"）不再成立**，冲频重新推进：

**阶段4 完成（2026-07-20）✅ 150M→400M 逐档全绿** —— 详见
`proposals/39-满血H743函数级trace冲频成果与产品化带宽分析.md`：
- 150M/200M/300M/400M 逐档，BB-OFF+cache，**全档 0 ETF 溢出、调用边逐条对 ELF**（400M
  1219/1219）、CoreMark 分数严格线性（611→1631）。
- 端口带宽利用率 7.7%（150/200M）→ 15%（300M）→ 25%（400M）——**BB-OFF 消除带宽瓶颈**。
- 480M 未达成：H743 官方上限 400M（PLL 锁定但 CPU 实跑 ~400M）。
- **原方案"追平满血"目标（函数级、满血负载、低错、调用图正确）达成**——不是靠"追平 480M
  全指令无损"（那不可达，R2 证），而是**400M 满血 + BB-OFF 函数级 + 逐条对上 ELF**。

**产品化定位（proposal 39）**：推荐 **200-300M + 2-bit TRACE**（线束 4+1→2+1），带宽裕量充分，
不需 VOS0 极限配置。400M 作为能力上限验证保留。

**仍有效的前序结论**：R2 的生成率真值（G50=17.9/G75=22.5 MB/s、BB=1 峰值簇溢出）是采集侧
测量，不依赖 mem 镜像，**不受 bug 影响，仍成立**——它正确地证明了"BB=1 全指令无损追平不可达"，
这与"BB-OFF 函数级追平可达"并不矛盾（两个不同目标）。
