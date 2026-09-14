# 提案 21：2-bit 降频 DDR trace 落地（用户拍板，绕开 r21 三层质疑）

> 用户决策：要**完整函数级 trace**（不是 PC 采样）、走 **2-bit 并口**、**先给 CPU 降频**、
> **DDR 采样**、找**对 SI 友好的频率**。
>
> 这个组合**正面化解了 r21 的三层质疑**：
> - r21 Q1（满速编码密度外推存疑）→ **降频后不追满速**，码率直接由降后的 CPU 频率决定，
>   不靠外推。
> - r21 Q2（SDR/DDR 未验）→ **采用 DDR**（已查实 STM32 TPIU 是 edge-aligned DDR，见 §2），
>   且现成捕获前端本就按 DDR 设计。
> - r21 Q3（TRACECLK 50-75MHz 纸面值）→ **不追高频**，降频让 TRACECLK 落到 SI 友好的
>   ~10-15MHz，远低于 r18 的 GPIO Fmax 50MHz。
>
> 一句话：**降频是这一刀的关键**——它把"满速 + 高 TRACECLK + 赌 DDR 余量"换成"低速 +
> 低 TRACECLK + 不开 stall 拿完整流"，r21 的纸面赌注全部落地为可控实测点。

---

## 1. 几乎全是现成件（不造轮子）

实测核对了仓库现有资产，2-bit 链路的 RTL **基本不用新写**：

| 件 | 现状 | 2-bit 改动 |
|----|------|-----------|
| `trace_capture_a7.v` | 源同步 **DDR** 捕获前端，OVERSAMPLE（默认，已上板验证）+ IDDR 两模式，输出 `{trace_a,trace_b}`（上沿/下沿 nibble） | 数据线宽 4→2（只接 trace_data[1:0]），其余不动 |
| `traceIF.v`（orbtrace 上游） | **原生支持 1/2/4-bit**（`width` 参数，CoreSight TPIU-Lite TRM） | `width=2'b10`（2-bit），零改 |
| `trace_stream_top.v` | 4-bit 抓取顶层（BRAM+UDP+OVERSAMPLE+IDELAY+**FPGA timebase**） | 顶层 trace_data_in 4→2，传 width=2 给 traceIF |
| 引脚（`trace_stream.xdc`） | TRACECLK=D17(MRCC)，TRACEDATA[0..3]=F13/E14/D14/E16，stage4 验证过 | 只用 D17 + F13/E14（D0/D1），**零新引脚** |
| FPGA timebase | proposal 18 §9.3 已实现并验证 | 直接复用，给函数级 trace 配真实时间轴 |
| 解码 | etm35lib / orbetto 函数级调用栈 | 与 SWO 路同源，直接复用 |

**新写的只有：STM32 侧 2-bit 并口 ETM 配置（改现有 etm_enable.cfg）+ 顶层 2-bit 例化 +
降频。**

---

## 2. STM32 TPIU 是 edge-aligned DDR（已查实，回应 r21 Q2）

`trace_capture_a7.v` 注释 + ARM CoreSight TRM 双重确认：**STM32 TPIU 的 TRACEDATA 在
TRACECLK 双沿都翻转（DDR），且 edge-aligned**（数据沿与时钟沿对齐，不是 centre-aligned）。

后果（决定采样方式）：
- **不能用 IDDR 在 TRACECLK 边沿直接采**（edge-aligned 会采在数据跳变点，falling 沿尤其错，
  代码注释明写）。
- **用 OVERSAMPLE**：ref_200m 过采样 TRACECLK + 数据，检测边沿后 mid-eye 延迟 EYE 个 ref
  周期再 latch。这本质就是双沿(DDR)捕获，只是 mid-eye 而非边沿采。**这套已上板验证（SWO
  与并口共用同一前端）。**

所以"DDR 采样"用户要求 = OVERSAMPLE 前端已在做。r21 Q2 的 SDR/DDR 敞口**用代码注释 + TRM
关闭**：是 DDR，且前端按 DDR 设计。

---

## 3. SI 友好工作点（降频，待实测锚定）

### 3.1 2-bit DDR 带宽

每 TRACECLK 周期 = 2 沿 × 2 线 = 4 bit：

| TRACECLK | 2-bit DDR 带宽 |
|----------|---------------|
| 6 MHz | 24 Mbit/s |
| **10 MHz** | **40 Mbit/s** |
| **15 MHz** | **60 Mbit/s** |
| 25 MHz | 100 Mbit/s |

### 3.2 降频后的 ETM 码率（估算，br_out=0 完整流）

ETM 码率 ≈ CPU_MHz × 0.78（IPC~0.7 × 1.12 bit/指令，**proj_add 量级；真实调用密集代码更
高，是上界偏乐观——这是 r21 Q1 仍适用的诚实边界**）：

| CPU 频率 | ETM 码率（估） | vs 2-bit DDR |
|----------|---------------|--------------|
| 24 MHz | ~19 Mbit/s | TRACECLK 6M(24) 够 |
| **48 MHz** | **~37 Mbit/s** | **TRACECLK 10-15M(40-60) 够，不 stall** |
| 72 MHz | ~56 Mbit/s | TRACECLK 15M(60) 边界 |
| 168 MHz | ~131 Mbit/s | 超，需 stall 或更高 TRACECLK |

### 3.3 推荐起点（保守、SI 友好、不 stall）

> **关键事实修正（实测依据：`target/downclock.cfg`）**：F429 的并口 **TRACECLK 直接由
> HCLK 派生，没有独立分频器**（TPIU_ACPR 只对 SWO 异步模式有效，对并口无效）。所以**唯一
> 的频率旋钮是 RCC_CFGR.HPRE 分频 HCLK**——降 HCLK 同时降 CPU 算力和 TRACECLK。
>
> 这反而让方案更干净：**CPU 算力与 trace 带宽天然同比例缩放、自动匹配**。不存在"满速 CPU +
> 慢 trace"（那只能靠 stall）。用户"先降频"的策略正好是并口唯一可行的非 stall 路。

- **HPRE 降频**：`DIV=4`（HCLK 168→42 MHz）或 `DIV=8`（→21 MHz）。降频用现成
  `downclock.cfg`（不 reset，避免固件 restore HPRE）。
- **TRACECLK 随之降**到 ~10-21 MHz 区间（DDR 下 TRACECLK 与 HCLK 的确切比值 = HCLK 或
  HCLK/2，**待示波器实测确认**），远低于 r18 GPIO Fmax 50M、r16 出问题频率 → SI 友好。
- **2-bit DDR 带宽与 ETM 码率同比例缩放**：降频后 CPU 慢了，ETM 码率也按比例降，2-bit 通道
  带宽（随 TRACECLK）同步降——两者比值不变。**所以"够不够"由 2-bit DDR vs ETM 码率的
  比值决定，与降频倍数无关**（§3.1/§3.2 的比值在任何 HPRE 下都成立）。
- **br_out=0**（只要调用栈，省带宽）。

> 重新表述核心权衡：降频不改变"2-bit DDR 带宽 vs ETM 码率"的比值，它只把两者一起拉到 SI
> 友好的低频。**真正决定不-stall 可行性的，是 2-bit DDR 每 HCLK 能搬多少 bit vs ETM 每
> CPU 周期产多少 bit**——这个比值 §3.1（4 bit/TRACECLK 周期）vs §3.2（~0.78 bit/CPU周期）
> 才是关键，PoC 要量的就是它。

---

## 4. 落地步骤

1. **STM32 侧**（改 `etm_enable.cfg` → `etm_2bit.cfg`）：
   - 降频：改 RCC（PLL N/P 或直接 HSI 48M），CPU → 48 MHz。
   - `DBGMCU_CR(0xE0042004)` TRACE_MODE=01（2-bit）→ bit5 IOEN + bits[7:6]=01 = `0x60`。
   - `TPIU_CSPSR(0xE0040004)` = `0x2`（port size 2-bit）。
   - `TPIU_ACPR/prescale` 设 TRACECLK ~12-15 MHz。
   - GPIO：只配 PE2(TRACECLK)/PE3(D0)/PE4(D1) AF0 + very-high-speed。
   - ETM：br_out=0 + 先开 stall 验证链路，再关 stall 测不-stall 完整流。
2. **FPGA 侧**：`trace_stream_top` 例化 trace_data 宽度 2、traceIF width=2；复用
   OVERSAMPLE 前端 + timebase；综合 2-bit 顶层。引脚只接 D17+F13+E14。
3. **解码**：现有 etm35lib/orbetto，函数级调用栈 + FPGA 时间戳（真实时间轴）。
4. **判收**：与 SWO 黄金基线（同固件 SWO 解出的调用栈）逐函数比对；OVERSAMPLE 的 duty/
   glitch 统计 + EYE 扫描定 mid-eye 点（前端已有这些诊断输出）。

---

## 5. 与 r21 的关系（诚实收口）

- r21 把 proposal 19 的"满速 2-bit 甜点"证伪降级——**那个降级仍成立**（满速 + 高 TRACECLK
  + 赌 DDR 是纸面的）。
- 本提案**不是翻案**，是**改命题**：从"满速不 stall"改成"**降频不 stall**"。降频把 r21 的
  三层赌注（外推/DDR/高频）全部换成低速可控实测点。
- 仍保留的诚实边界（r21 Q1 残留）：§3.2 码率估算偏乐观，真实代码可能更高 → 但降频下有连续
  旋钮兜底，不是 all-or-nothing。
- P-header 砍不掉（r21 Q5）依旧成立：完整函数级流必然带 P-header，这是 ETM-M4 硬约束。
  2-bit 降频是"用够用的低速通道 + 不拖慢 CPU 装下它"，不是把流变小。

---

## 6. 待实测（PoC 第一步，低成本）

1. **配 2-bit + 降频 48M + TRACECLK 12M，先开 stall 抓一段**：验证 2-bit 链路解出函数级流
   （与 SWO 基线比对）。证明 2-bit 采样 + traceIF width=2 正确。
2. **关 stall 再抓**：看 48M/不-stall 下是否丢包（overflow packet / unknown%）。不丢 → 拿到
   "不被拖慢的完整函数级 trace"，目标达成。丢 → 调旋钮（降 CPU / 升 TRACECLK）。
3. **EYE 扫描 + duty/glitch 统计**：定 mid-eye 采样点，量 SI 余量（前端诊断已有）。
4. 升 TRACECLK 找 SI 上限（眼图判收），定这套连接的 2-bit 实际带宽天花板。
