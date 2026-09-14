# STM32F429 SWO 带宽上限分析（追平/超越 ORBTrace 48Mbps 的可行性）

> 问题：我们的 A7 FPGA 采集端性能强，能否追平甚至超越 ORBTrace 的 SWO 性能
> （NRZ 62Mbaud / Manchester 48Mbit/s）？瓶颈在 FPGA 采集端，还是 F429 目标端？
>
> 结论先行：**FPGA 采集端能追平甚至超越**（IDDR 双沿→500MSa/s 追平 ORBTrace，
> ISERDES→1GSa/s 超越）。**但 F429 这颗目标芯片本身的 SWO 输出上限约 50–90MHz
> （GPIO 引脚 Fmax 限制），且 ACPR 整数分频只能取 HCLK 的整数分频点。实际可用上
> 限受"GPIO Fmax + stall 后 CPU 能否喂满"双重约束。**

---

## 1. SWO 速率是怎么决定的（三段约束，取最小）

```
SWO baud = TRACECLKIN / (ACPR + 1) = HCLK / (ACPR + 1)
```

| 约束 | 数值（F429） | 依据 |
|------|-------------|------|
| ① TPIU 本身 | 无额外上限，ACPR=0 → HCLK/1 | ARM TRM: TPIU_ACPR "no usage constraints"；divisor=ACPR+1 |
| ② HCLK 上限 | 180 MHz（我们跑 168 MHz） | F429 datasheet 最高主频 |
| ③ **GPIO 引脚 Fmax** | **见下表（真正的物理墙）** | F429 datasheet I/O AC characteristics |

### ③ GPIO Fmax（OSPEEDR=11 very-high-speed，VDD≥2.7V，F429 datasheet）

| 负载 CL | 最大翻转频率 Fmax |
|---------|------------------|
| 10 pF | 100 MHz |
| 30 pF | 90 MHz |
| 50 pF | 50 MHz |

> SWO 是 NRZ/UART 波形，一个 bit = 引脚一次电平保持，最坏情况 `0101…` 每 bit 翻转
> 一次 → **Fmax 直接就是 SWO 的 baud 上限**。所以 F429 在轻载（短线/低电容）下
> SWO 物理上限 ~**90–100 MHz**，重载（长杜邦线、50pF）掉到 ~**50 MHz**。

### ACPR 整数分频的可取点（HCLK=168MHz）

只能取 168/(N) 的整数分频：

| ACPR | divisor | SWO baud |
|------|---------|----------|
| 0 | 1 | 168 MHz ❌ 超 GPIO Fmax |
| 1 | 2 | 84 MHz ⚠️ 接近/超轻载 Fmax |
| 2 | 3 | 56 MHz ✅ 轻载可行 |
| 3 | 4 | 42 MHz ✅ |
| 5 | 6 | 28 MHz ✅ |
| 7 | 8 | 21 MHz ✅（我们已实测干净）|

> 注：把 HCLK 调到别的值（如 160/192MHz 若 PLL 允许）能得到不同分频点，
> 但 GPIO Fmax 的墙不变。

---

## 2. FPGA 采集端：我们 vs ORBTrace

| | ORBTrace (ECP5) | 我们现状 (A7) | 我们可达 (A7) |
|---|---|---|---|
| 采样时钟 | swo2x 250 MHz | ref 200 MHz | MMCM 可到 ~400 MHz |
| 每周期采样 | **2**（IDDR 双沿，`In(2)`） | 1（单沿） | 2（IDDR）/ 4–8（ISERDES） |
| **等效采样率** | **500 MSa/s** | 200 MSa/s | **500 MSa/s（IDDR）/ ~1 GSa/s（ISERDES）** |
| 实测/理论 NRZ 上限 | 62 Mbaud | 21 MHz（实测干净）| ~62M（IDDR）/ >100M（ISERDES） |
| 解码原理 | 脉宽测量 | 脉宽测量（同） | 同 |

**关键认知**：ORBTrace 的 48Mbps **不是靠每 bit 过采样很多次**，而是靠
**500 MSa/s 等效采样率 + 脉宽测量**——48Mbit Manchester（symbol ~10ns）只有
~5 个采样/symbol。我们现在单沿 200MSa/s 是它的 1/2.5，所以卡在 ~21M。

**追平/超越的路径（A7 比 ECP5 强，这是确定的）**：
1. **IDDR 双沿**（已有原语 IDDR + IDELAYE2）：250MHz×2 = 500MSa/s = **与 ORBTrace 同档** → ~62M NRZ。改动小。
2. **提采样时钟**：MMCM 拉到 300–400MHz，单沿即 300–400MSa/s。
3. **ISERDESE2 1:4/1:8 解串**（A7 专为高速单线 SerDes 设计，注释里自己写了
   "ISERDES makes sense for >500Mbps single-lane"——SWO 单线正是此场景）：
   等效 ~1 GSa/s → 理论 **>100M NRZ，超越 ORBTrace**。

---

## 3. 谁是真正的瓶颈？

把三段墙叠起来（轻载 30pF 场景）：

```
FPGA 采集端：   200MSa/s(现) → 500MSa/s(IDDR) → ~1GSa/s(ISERDES)
                对应 NRZ:  21M  →  ~62M  →  >100M
                                    │
F429 GPIO Fmax：~90MHz ───────────┤  ← 目标端物理墙先到
                                    │
ACPR 分频可取点：…42M / 56M / 84M…  │
```

- **当前（200MSa/s）**：瓶颈是**我们 FPGA 采集端（21M）**。
- **升级到 IDDR 500MSa/s 后**：瓶颈转移到 **F429 GPIO Fmax（~50–90M）+ ACPR 分频点**。
  此时能跑到 **56M（ACPR=2，轻载）**，**已超越 ORBTrace 的 48Mbit/s Manchester 有效吞吐**。
- **ISERDES（~1GSa/s）**：采集端不再是瓶颈，**完全由 F429 GPIO Fmax 封顶（~90M 轻载）**。
  对 F429 而言用不满，但换 SWO 驱动更强的目标（H7/H5）即可逼近 ISERDES 的 >100M。

### 还有一个隐性约束：stall 后 CPU 能否喂满

ETM full-trace 必须开 stall，CPU 被 trace 带宽拖慢。baud 越高，FIFO 排空越快、
CPU 跑得越快、瞬时码率越高。但 F429 满速 ETM 峰值码率 >> 任何 SWO baud，所以
**SWO 始终是瓶颈、stall 始终在限速**——这意味着无论多高 baud，都是"以 stall 拖慢
CPU 换取不丢包"，48M vs 21M 的差别是 CPU 被拖慢的程度（~1/7 vs 1/20 速）。

---

## 4. 结论与建议

| 问题 | 答案 |
|------|------|
| FPGA 采集端能否追平 ORBTrace 48Mbps？ | **能**。IDDR 双沿 500MSa/s 直接同档，原语已有，改动小。 |
| 能否超越？ | **采集端能**（ISERDES ~1GSa/s）。但 **F429 目标端 GPIO Fmax ~90M 会先封顶**。 |
| F429 SWO 实际上限？ | **~50M（重载/长线）到 ~90M（轻载/短线）**，受 GPIO Fmax；ACPR 整数分频实际落点 42M/56M/84M。 |
| 用别的采样方案？ | 是。从"单沿过采样"换成 **IDDR 双沿脉宽测量**（追平）或 **ISERDES 解串**（超越），后者正是 SWO 单线高速的正解。 |

**推荐路径**：
1. 先实现 **IDDR 双沿 SWO 前端**（500MSa/s，原语现成，追平 ORBTrace），实测 F429
   能驱动到哪个 ACPR 分频点（预期 28M/42M 干净，56M 临界，受 GPIO Fmax + 线缆）。
2. 若要逼近/超越 ORBTrace 并验证 ISERDES 的 >100M，需换 SWO 驱动更强、HCLK 更高的
   目标（H7 480MHz / H5），F429 的 GPIO Fmax 用不满 ISERDES。

> 一句话：**采集端我们能追平（IDDR）甚至超越（ISERDES）ORBTrace；F429 这颗芯片自己
> 的 SWO 物理上限（GPIO Fmax ~50–90M）会成为新的瓶颈，落在 ORBTrace 同一量级。要真正
> 跑出 >100M 看到 ISERDES 的威力，得换更强的目标芯片。**

---

## 附：数据来源

- F429 GPIO Fmax（10/30/50 pF → 100/90/50 MHz）：STM32F427/F429 datasheet,
  I/O AC characteristics（OSPEEDR=very high speed, VDD≥2.7V）。数据为 ST 官方规格，
  本文转述用于分析。
- TPIU_ACPR 无使用约束、divisor=ACPR+1：ARM Cortex-M TRM, TPIU programmer's model。
- ORBTrace 62Mbaud NRZ / 96Mbaud(48Mbit/s) Manchester、采样 250/125MHz IDDR(In(2)
  =500MSa/s)：orbcode.org 文章 + orbtrace `crg_ecp5.py` / `trace/swo.py` 源码。
- 我们实测 NRZ 干净到 21MHz（自适应重锁，0.28% unknown）：本仓库 doc 15 / r17。
