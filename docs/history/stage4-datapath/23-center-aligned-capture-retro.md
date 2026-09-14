# Stage-4 · 采集相位复盘：TPIU 数据是 center-aligned，IDELAY/MMCM 全是弯路

> 日期：2026-09-07
> 结论一句话（**已按扫频实测修正**）：STM32H743 TPIU 数据相对 TRACECLK 的对齐
> **是频率相关的**——低频（≤77MHz pin）稳定 **center-aligned**（IDDR 直接采命中
> 眼中心，IDELAY/MMCM 不需要）；高频（112MHz pin）退化到 **intermediate/偏
> edge**（对齐度 0.42），此时相位补偿才有意义。**弯路的真正错误不是"用了
> IDELAY"，而是"在低频档也无脑套 IDELAY/MMCM，且没意识到是否需要补偿是频率
> 相关的"**。

## 0. 扫频实测（这是最硬的一张表，推翻了初版"IDELAY 全是弯路"的过度结论）

`phase_sweep.py`（一次性脚本）用 CLI 逐档改 PLL，每档读 CH1(TRACECK)+CH2(TRACED)
原始模拟波形，统计数据边沿到最近时钟边沿的距离（0=edge-aligned，1=正中眼心）：

| R | pin MHz | UI (ns) | 对齐度 | 判决 |
|---|--------:|--------:|-------:|------|
| 2 | 113 | 8.84 | **0.42** | INTERMED（偏 edge）|
| 3 | 77 | 13.0 | 0.83 | CENTER |
| 4 | 56 | 17.8 | 0.82 | CENTER |
| 6 | 37.5 | 26.6 | 0.85 | CENTER |
| 8 | 28 | 35.4 | 0.83 | CENTER |
| 12 | 18.5 | 53.9 | 0.94 | CENTER |
| 16 | 14 | 71.1 | 0.96 | CENTER |

**物理机制（再次修正 —— 高频"偏 edge"其实是 SI 眼闭伪象，不是相位移动）**：

`si_check.py` 量 CH2 数据 lane 的 rise/fall time 与稳定平台 dwell：

| pin MHz | UI (ns) | rise (ns) | fall (ns) | 边沿占 UI | 稳定平台 | 判决 |
|--------:|--------:|----------:|----------:|----------:|---------:|------|
| 113 | 4.40 | 2.96 | 3.00 | **67.7%** | **1.42 ns** | SI 眼闭 |
| 57 | 8.80 | 3.10 | 3.10 | 35% | 5.70 ns | SI 边缘 |
| 28 | 18.0 | 3.6 | (10.8*) | 40% | 10.8 ns | SI 边缘 |

- **rise/fall ≈ 固定 3 ns**（板 + 飞线的 SI/驱动上限，与频率无关）。
- 113MHz 时 UI 仅 4.40ns，边沿吃掉 ~6ns > 一个 UI，**稳定平台只剩 1.42ns**。
- 所以过零统计里的"偏 edge（对齐度 0.42）"**不是相位挪了，是眼睛闭合**：数据
  还在爬升，下一个 UI 就到，IDDR 采到的是斜坡上的电平。

**这改写了 IDELAY 的定位**：IDELAY 只能移采样相位，**移不出一个不存在的眼**。
112MHz 的 1.42ns 稳定窗对 3ns 边沿 + 抖动，怎么移都在斜坡上。所以：
- **IDELAY 在 112MHz 救不了**（不是"高频有用"，是根本没眼可采）。
- 真正的解只有两条：①**降频**（≤57MHz 让 UI 容纳 3ns 边沿）②**改 SI**（换低阻
  驱动 / 短走线 / 端接，把 rise time 压到 <1ns）——板级工作。
- 固定 3ns rise time = 这块板 + 飞线的物理天花板。

（* R=8 fall=10.8ns 是那次抓到的下降沿慢/下冲，与之前 SI 复盘的 fall 不对称
  吻合，但 rise=3.6ns 可信；不影响"边沿占 UI"的结论。）

**这同时解释了三个历史谜团**：
1. 早期另一台 LA 看到 "CLK/DATA 一起动" —— 那是高频档（≥112MHz），确实接近 edge。
2. 28MHz 下 IDDR 直接采就行 —— 低频稳 center。
3. direct 前端 112MHz 只出 934 FSYNC、56MHz 出 20538 —— 112MHz 相位裕度差（0.42），
   采集质量掉。

**推论（终版）**：
- **≤57MHz：眼开，IDDR 直采即可**（center-aligned + 稳定平台足够），
  `trace_capture_direct` 足矣，IDELAY/MMCM 不需要。
- **112MHz：眼闭（SI），IDELAY 也救不了**。要么降频，要么板级改 SI（rise<1ns）。
  当前板 + 飞线的固定 3ns 边沿 = 112MHz 4-bit 并口的物理上限。
- **"拿个 MCU 采集够不够"**：低频 center-aligned 时逻辑上可行，但 MCU 无源同步
  采样硬件、GPIO/DMA 采样率跟不上 DDR 数据率。而且**高频段真正的瓶颈是 SI 不是
  采集芯片**——换 FPGA 也没用，除非先解决 3ns 边沿。FPGA 的价值仅在"SI 够好、
  UI 容得下边沿"的高频段（需要板级把 rise time 压下去后才谈得上 200MHz+）。

---

## 1. 实测证据

`phase_check.py`（已删的一次性脚本）直接读 CH1(TRACECK)+CH2(TRACED) 的**原始
模拟波形**（无 LA、无 `LA_SAMPLE_FRAC` 软件偏移），在 1.65V 阈值做插值过零，
统计每个数据边沿到最近时钟边沿的距离：

```
pin 时钟 28.2 MHz (= pll1_r_ck 56.25MHz / 2)
数据边沿到最近时钟边沿的中位距离 = 0.84 个半 UI
=> CENTER-ALIGNED：时钟边沿在数据眼中心
```

对照两个采集前端上板抓的 raw（56 MHz 档，0.2s）：

| 前端 | recover_assemble 后 FSYNC | 说明 |
|------|--------------------------:|------|
| `trace_capture_a7`（IDELAY tap=2） | ~同量级 | raw 头字节 `f7ff…`，与 direct 同构 |
| `trace_capture_direct`（无 IDELAY） | 20538 | 加不加 IDELAY 一样好 |

**两个前端 raw 逐字节同构** → IDELAY 对结果零贡献，坐实"时钟边沿本就在眼中心"。

三方独立路径在 56 MHz 都恢复出真 ETMv4 A-sync：

| 路径 | ETM 字节 | A-sync |
|------|---------:|-------:|
| Golden (ETF-DAP over SWD) | 4096 | 4 |
| FPGA (direct bit, 0.2s) | 6.36 MB | 459 |
| LA (STM32 pin) | 9.7 KB | 9 |

---

## 2. 错在哪

历史文档（`trace_capture_a7.v` 注释、doc 14 §21、doc 16）反复写：

> "The STM32 TPIU is EDGE-aligned (ARM CoreSight TRM: traceclk edges are not
>  offset from data edges), so IDDR samples right on the data transition."

**这是误读。** 实测 STM32H743 的 TPIU 是 center-aligned（TRACEDATA 相对 TRACECLK
setup/hold 对称，跳变居中）。基于错误假设衍生了两套机制：

1. **IDELAYE2 per-lane tap 扫描**（`iddr_tap_sweep.py` / `freq_ceiling_sweep.py`）：
   给数据线加延迟把采样点"推进眼中心"。但眼中心本就在时钟边沿，tap 扫描
   只是在把一个已经对的相位挪来挪去，还引入了**频率耦合**（tap 是固定 ps 延迟，
   换频率就要重扫）。
2. **MMCM 90° 相移采样时钟**（`trace_capture_mmcm.v` / `trace_mmcm_stream_top.v`）：
   生成移相 90° 的采样时钟"落到眼中心"。同样是解决一个不存在的问题，还带来
   低频 MMCM 锁不上、PHASE 编译期锁死等新麻烦。

## 3. upstream orbtrace 早就是对的

`refs/orbtrace-upstream/orbtrace/trace/glue.py` 采集端就三行：

```python
DDRInput(clk=traceclk, i=tracedata[i], o1=trace_a[i], o2=trace_b[i])
```

**无 IDELAY、无相移**，直接在 TRACECLK 边沿双沿采。之所以行，正是因为数据
center-aligned + IOB 走线延迟让采样点落在眼内。我们的 `trace_capture_direct.v`
就是它的忠实 Xilinx 移植，上板验证等效。

所谓 "IDDRX1F 内建 90° 相移" 也是误解——普通 DDR 触发器，没有相移。

## 4. TRACECLK = pll1_r_ck / 2（顺带修正）

同一轮实测还纠正了一个频率换算错：pin 上的 TRACECLK = pll1_r_ck / **2**。
- R=2：pll1_r_ck 225MHz → TRACECLK pin **112MHz**
- R=8：pll1_r_ck 56.25MHz → TRACECLK pin **28MHz**（示波器实测 28.2MHz）

TPIU 并口 DDR：pin 时钟跑内部 bit-clock 的一半，数据双沿。`pll_ctrl_traceclk_hz()`
已修为 `pll1_r_ck / 2`。（此前一次重构误把 pll1_r_ck 当 pin TRACECLK，依据是被
采样率搞混的 LA 读数——已撤回。）

## 5. 行动项

- [x] `trace_capture_a7` 加 `USE_IDELAY=0` 旁路（IBUF→IDDR 直连），综合+上板验证
- [x] `pll_ctrl_traceclk_hz` 修为 /2
- [ ] **P1 减法（修正为：保留高频补偿能力，删重复实现）**。IDELAY 在 ≥112MHz
  仍有用，不能全删。但三套并存的相位机制是冗余：
  - **保留**：`trace_capture_a7.v` 的 IDELAY 分支（高频补偿）+ `USE_IDELAY=0`
    旁路（低频直采）—— 一个模块两条路径，已经够。
  - **删**：`trace_capture_mmcm.v` / `trace_mmcm_stream_top.v` / `trace_mmcm_top.v`
    （MMCM 90° 相移是 IDELAY 的重复方案，且低频锁不上）。
  - **删**：`eyescan_top.v` / `trace_pin_la_top.v` 等一次性相位实验 top。
  - **保留但归位**：`iddr_tap_sweep.py`（高频找眼心仍需要，但要标注"仅 ≥100MHz
    有意义"，加频率 guard）。
- [ ] **默认策略**：低频（≤77MHz）默认 `USE_IDELAY=0` 直采；高频再开 IDELAY。
- [ ] 独立战线（与采集无关）：cortrace/libopencsd 在 ~64-196KB 处 fatal，
  deframe 后仍掺 HSYNC `0xf7` 残留——PC 端解码质量问题。

## 6. 教训

**一个未经实测的物理假设（"edge-aligned"）繁殖出两套复杂子系统。** 复盘方法论
（AGENT.md §6）本该更早用示波器 raw 波形量一次相位——30 秒的实验，省掉几个月的
IDELAY/MMCM 弯路。center-aligned 这个结论用最原始的手段（CH1/CH2 原始波形过零
统计）一测即知，不依赖任何软件重组或猜测。
