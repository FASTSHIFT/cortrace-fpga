# 提案 15：把 ORBTrace 的 SWO 单线 trace 采集能力移植到 Artix-7

> **触发背景**：并口 5 线 trace 在设备重启后出现采集端 SI 问题（实测：LA 金标准能 0.002% 解出真实 PC，
> 而 FPGA 采集端看到 TRACECLK 下降沿位置异常——LOW 半位被压到 155ns，HIGH 稳定 385ns——任何 EYE/order
> 都救不回，怀疑重启动到了排线）。SWO 是**单线**，把并口最难的"源同步 + 通道偏斜"整类 SI 问题直接消掉。
> 本提案评估把 orbtrace 上游成熟的 SWO 采集链路移植到我们 Artix-7 bringup 工程的可行性，并给出落地方案。
>
> **一句话结论**：**可行，且工作量小、风险低、复用度极高**。SWO 解码本质就是"单线过采样 → 脉宽 →
> 位 → 字节"，而我们的 OVERSAMPLE 前端（ref_200m 过采样取脉宽/眼心）已经把最难的采样部分做完了。
> 上游 orbtrace 的 amaranth SWO 模块（`PulseLengthCapture`/`NRZDecoder`/`UARTDecoder`/`ManchesterDecoder`）
> 是经过验证的参考实现，直接照搬其状态机到 Verilog 即可。SWO 支线研究（`docs/swo-trace-sidetrack`）已
> 在真实 STM32F429 上把"SWO→ETM 指令流"全链路跑通（0.002% 命中真实 PC），为本移植提供了金标准对照。

---

## 1. 为什么现在做 SWO

### 1.1 并口 SI 困境（实测）

| 证据 | 数据 |
|------|------|
| LA 金标准（独立采集路径）| `parity=0 order=low` + TPIU deframe → **0.002% unknown**，820 锚点全是真实 PC（0x08000f8c add / 0x08000fb2 loop_sum）|
| FPGA 采集端（重启后）| 任何 EYE/parity/order 最好 15% unknown、TPIU sync 数仅 1/4，stray PC 全是 `0x0808` 噪声 |
| FPGA duty 测量 | HIGH 半位稳定 375-385ns，**LOW 半位 155-385ns（下降沿位置异常）**，glitch_cnt≈1 |

→ STM32 ETM 输出**完全正常**（LA 证明），坏在 **FPGA 这段物理链路**（5 线源同步采样的 SI）。这正是
SWO 支线 §17.3 早就预言的"并口 SI 是命门"。

### 1.2 SWO 把 SI 难题整类消除

| SI 问题 | 并口 trace（TRACECLK+4×DATA）| SWO 单线 |
|---------|------------------------------|---------|
| 通道间偏斜(skew) | ⚠️ 致命，5 线须等长 | ✅ 不存在（1 线）|
| 源同步 setup/hold | ⚠️ 随频率收紧 | ✅ 不存在（异步）|
| 多线串扰 | ⚠️ 5 线并行 | ✅ 单线 |
| 独立时钟线质量/抖动 | ⚠️ TRACECLK 直接限速 | ✅ 无独立时钟线 |
| 控阻抗根数 | 5 根 | **1 根** |

我们当前的卡点（重启后 TRACECLK 下降沿失真）在 SWO 下**根本不存在**——SWO 没有独立时钟线，
解码靠脉宽/波特率自恢复。

### 1.3 我们已经具备的基础

SWO 支线研究（`docs/swo-trace-sidetrack/README.md`）已实测确认：
- STM32F429 的 SWO 唯一引脚 **PB3**（TRACESWO，复用 JTDO，SWD 模式下可用），ROM 表里 ITM/TPIU/ETM 齐全；
- ETM-over-SWO（formatter on，NRZ 2MHz）经 LA→orbuculum→ETMv3.5 解出 **真实指令流**，PC 100% 命中代码段；
- 解码链路（TPIU 去帧 + ETMv3.5）我们这边已有 `etm35lib.py` 完整实现，本就在用。

**所以缺的只有一件事：FPGA 把 PB3 那根线采下来、解成字节流，喂进我们已有的下游。**

---

## 2. SWO 解码链路拆解（移植对象）

ORBTrace 上游 `orbtrace/trace/swo.py`（amaranth）的链路：

```
SWO 引脚 ──2x过采样──> PulseLengthCapture ──脉宽流──> ┬─ NRZDecoder ─> UARTDecoder ─> 字节
                                                      └─ ManchesterDecoder ─> BitsToBytes ─> 字节
```

各模块职责（已读源码 + 上游单测确认语义）：

| 模块 | 输入 → 输出 | 作用 | 复杂度 |
|------|-----------|------|--------|
| `PulseLengthCapture` | 2bit 过采样 → `{level, count16}` 脉宽流 | 把"采样位流"压成"电平+持续长度"，含 1 个采样的毛刺滤除 | **低**（~40 行状态机）|
| `NRZDecoder` | 脉宽流 → 1bit 流 | 按 `bitlen`（波特率）把脉宽切成 N 个 UART 位 | **低**（累加器减 bitlen）|
| `UARTDecoder` | 1bit 流 → 8bit | 标准 8N1 起始位/移位/停止位 | **低**（10bit 移位寄存器）|
| `ManchesterDecoder` | 脉宽流 → 1bit(带 first) | 曼彻斯特自同步解码（short/long/extra-long 阈值 FSM）| 中 |
| `BitsToBytes` | 1bit(带 first) → 8bit | 曼彻斯特位流按帧首拼字节 | 低 |

**对 F429 我们只需要 NRZ 路径**（`PulseLengthCapture → NRZDecoder → UARTDecoder`）：
- 支线 §2 实测 F429 用的是 **NRZ(UART) SWO**（`TPIU_SPPR=2`），不是 Manchester；
- Manchester 是 ORBTrace mini 那种高速专用编码，F429 配 NRZ 即可，普通 UART 语义。
- → **曼彻斯特链路（ManchesterDecoder + BitsToBytes）本期可不移植**，留作将来高速可选项。

### 2.1 关键洞察：我们的 OVERSAMPLE 前端已经做了一大半

`PulseLengthCapture` 做的事 = 在快时钟域过采样输入线、检测电平翻转、累计同电平的持续采样数。
**这正是我们 `trace_capture_a7.v` OVERSAMPLE 已经在 ref_200m(5ns) 域做的事**（我们检测 TRACECLK 边沿、
用计数器测半位 dwell——见现有 duty 测量逻辑 `dwell`/`duty_hi_sum`）。

> 也就是说，把 TRACECLK 边沿检测 + dwell 计数那套，从"测时钟占空比"改成"测 SWO 脉宽并输出脉宽流"，
> 就是 `PulseLengthCapture`。复用度极高，不是从零写。

---

## 3. 移植方案（Verilog，落地到 bringup 工程）

我们 bringup 是纯 Verilog + Vivado 流程（不用 amaranth），所以是**照着上游 amaranth 参考实现写等价
Verilog**，不是直接编译 amaranth。三个小模块：

### 3.1 `swo_pulse_capture.v`（对应 PulseLengthCapture）

```
输入：swo_in（PB3 单线，IBUF 后），ref_200m
输出：pulse_valid, pulse_level, pulse_count[15:0]
逻辑：3-FF 同步 swo_in 进 ref_200m 域；检测电平翻转；同电平累加 count；
      翻转或 count 溢出时输出 {level, count} 并清零；1 采样毛刺并入（照搬上游 Switch 表）。
```

- 采样时钟：直接用现有 `clk200`（ref_200m，5ns）。SWO 2MHz → 每位 500ns = 100 个 ref 周期，
  过采样率 100×，远超 Nyquist，眼图极宽。
- 这块和现有 duty 测量几乎同构，可从 `trace_capture_a7.v` 的 `dwell` 逻辑改出来。

### 3.2 `swo_nrz_decode.v`（对应 NRZDecoder）

```
输入：pulse 流，bitlen[15:0]（= ref周期数/UART位 = 200e6/baud；2MHz→100）
输出：bit_valid, bit_value
逻辑：收到一个脉宽，置 acc = count<<4 + bitlen/2；当 acc>=bitlen 且 cnt<12 时
      输出一个 bit(=level)，acc-=bitlen，cnt++。把"长脉宽"摊成连续 N 个相同 UART 位。
```

- `bitlen` 做成 CSR 可写（复用现有 :5002 控制口），运行时设波特率，无需重烧。

### 3.3 `swo_uart_decode.v`（对应 UARTDecoder）

```
输入：bit 流
输出：byte_valid, byte[7:0]
逻辑：等起始位(0) → 移入 8 位 → 停止位 → 输出字节（标准 8N1，LSB first）。
```

### 3.4 顶层集成

新建 `swo_stream_top.v`（或在现有 `trace_stream_top.v` 加 `SWO_MODE` 参数）：
- 把 `swo_pulse_capture → swo_nrz_decode → swo_uart_decode` 串起来，输出字节 `cap_byte/cap_valid`；
- **下游完全复用现有结构**：字节写进同一个 BRAM、同一个 UDP :5001 paged readout、同一个 FPGA 时间戳
  快照表（§24.2 的 `cap_clk_cnt`/`tsmem`，每 N 字节打 ref_200m 时间戳）。
- SWO 引脚用一根空闲 BANK16 IO（如复用现有 `trace_data_in[0]`=F13，或另指定），LVCMOS33。

> **复用清单**（几乎整条下游不动）：BRAM 抓取、UDP readout、CSR 控制口、FPGA 时间戳快照表、
> `trace_dump.py --timebase`、`etm35lib` TPIU 去帧 + ETMv3.5 解码、`etm_with_time.py`、
> `etm_to_perfetto.py` / orbetto 链路。**SWO 只是换了个"前端采集源"，后面全套照用。**

---

## 4. 数据通路对比

```
【并口（现状）】
STM32 ETM 4bit ─5线─> A7 OVERSAMPLE(源同步,SI命门) ─> {b,a}字节 ─> BRAM ─> UDP ─> 解码

【SWO（本提案）】
STM32 ETM ─TPIU formatter─> PB3 单线 NRZ ─1线─> A7 SWO采集(过采样,无SI命门)
   ─> 字节 ─> BRAM(复用) ─> UDP(复用) ─> 解码(复用)
```

两条路**下游字节流格式一致**（都是 TPIU formatter 帧，stream-2=ETM），所以解码侧零改动。

---

## 5. 工作量与风险评估

| 项 | 评估 | 说明 |
|----|------|------|
| RTL 新增 | **小**（3 个小模块 ~150 行 Verilog）| 照上游 amaranth 参考写等价 Verilog；PulseCapture 可从现有 duty 逻辑改 |
| 下游改动 | **几乎为零** | BRAM/UDP/时间戳/解码全复用 |
| 时序风险 | **低** | SWO 2MHz vs ref_200m，过采样 100×，无源同步约束、无 IDELAY、无 BUFR_IO 难点 |
| SI 风险 | **极低** | 单线，杜邦线即可（支线已用杜邦线在 LA 上验证）|
| 引脚 | 1 根 BANK16 IO + 共地 | 远少于并口 5 线 |
| 金标准对照 | **现成** | 支线 §6 的 LA→orbuculum 结果 + 我们 `etm35lib` 解码，可逐函数比对 |
| 可单元测试 | ✅ | 上游 `test_swo.py` 的向量可直接搬来做 Verilog testbench 的期望值 |

**主要不确定点（需实测验证，先标注为推断）：**
1. **波特率匹配**：NRZ 是异步的，CPU 变频会失锁（支线 §7 最大的坑）。我们定频 168MHz + 整数分频
   波特率（2M=/84）即可，和支线一致。→ 低风险但必须定频。
2. **带宽天花板**：SWO NRZ 实用上限受芯片驱动与我们采样率限制。ref_200m 过采样下，SWO 可到
   ~10-20MHz（半位 ≥10 个 ref 周期时眼图够）。支线实测 CH343 顶格 6MHz；我们 FPGA 采样率更高，
   理论可更快，但**满速 ETM 仍需 stall 节流**（这是 SWO 的固有限制，非移植问题）。
3. **stall 死锁风险**：开 stall 时若 SWO 没真输出，FIFO 满会卡死 CPU（支线 §7.2）。必须接好 NRST。

---

## 6. 落地步骤（建议）

1. **阶段 A：仿真先行**（零硬件）
   - 把上游 `test_swo.py` 的脉宽/字节向量做成 Verilog testbench 期望值；
   - 写 `swo_pulse_capture / swo_nrz_decode / swo_uart_decode` 并仿真通过；
   - 用支线已抓的 SWO 原始字节（或 LA 的 PB3 波形）灌进仿真，验证解出与金标准一致。
2. **阶段 B：上板**
   - 加 SWO 引脚约束（1 根 BANK16 IO），综合 `swo_stream_top`；
   - STM32 配 ETM-over-SWO（复用支线 §5 寄存器序列：`TPIU_SPPR=2` NRZ、`FFCR=0x102` formatter on、
     `ETMCR` br_out+stall、定频 168MHz、波特率 2M）；
   - 杜邦线接 PB3 → FPGA SWO 引脚 + 共地。
3. **阶段 C：验证**
   - `trace_dump.py` 抓字节 → `etm35lib` 去帧 + 解码 → 与支线 LA 金标准逐函数比对；
   - 接 FPGA 时间戳（§24.2）→ orbetto → Perfetto，验证带真实时间的调用栈。

---

## 7. 与主线的关系（定位）

- 这是**绕开当前并口 SI 卡点的低风险旁路**，让全链路（采集→时间戳→Perfetto 调用栈）在 SWO 上先
  端到端跑顺，把"采集源"和"下游解码/可视化"解耦验证。
- 并口 trace 仍是**满速、不可 stall 实时**的最终目标（SWO 带宽天花板决定）。SWO 移植后，正好可作为
  并口方案的**黄金对照基线**（支线 §17.4）：同一固件，SWO 解出的指令流当 ground truth，并口数据与之
  逐函数比对，用来判定并口 SI 是否真的采对了。
- 两者**共享整条下游**（BRAM/UDP/时间戳/解码/Perfetto），所以 SWO 这条不是"另起炉灶"，而是给同一
  套后端再接一个低 SI 的前端。

---

## 8. 结论

**强烈建议移植，且优先做 NRZ 路径。** 理由：
1. 把当前卡死我们的并口 SI 问题整类消除（单线、无源同步、无独立时钟）；
2. 工作量小（~150 行 Verilog，下游全复用），风险低（无 IDELAY/BUFR_IO/skew 难点）；
3. 复用度极高（前端换源，时间戳 + 解码 + Perfetto 全套照用，这些刚刚才打通并实测验证）；
4. 金标准现成（支线 LA 结果 + `etm35lib`），可逐函数判收；
5. 与主线不冲突，反而成为并口满速 PoC 的对照基线。

唯一要接受的代价是 SWO 的带宽天花板（满速 ETM 需 stall 节流），但这对"先把全链路在低 SI 前端上
跑通、验证时间戳 + 调用栈"的当前目标完全够用，且本就是 SWO 的已知固有特性，非移植引入。
