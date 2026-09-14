# Orbtrace Artix-7 移植项目文档

本目录记录将 ORBTrace 移植到 **Artix-7 + 千兆以太网出口**、用于 Cortex-M 指令流 trace 的全过程文档，包括方案设计、红蓝对抗评审、以及落地计划。

- Fork：`git@github.com:FASTSHIFT/orbtrace.git`（remote `fork`）
- 上游：`orbcode/orbtrace`（remote `origin`，保留同步能力）
- 开发分支：`artix7-port`

---

## 目录结构

```
docs/artix7-port/
├── README.md          # 本索引
├── PLAN.md            # 第一阶段计划书（无硬件仿真验证）✅
├── PLAN_STAGE2.md     # 第二阶段计划书（选板与 OOC 综合）✅
├── PLAN_STAGE4.md     # ★ 第四阶段计划书（trace 数据通路端到端打通）—— 当前在做
├── stage3-bringup/    # 第三阶段上板 bring-up 踩坑记录（点灯 / ETM / 网口）✅
├── proposals/         # 蓝方方案（按演进顺序 00→14）
└── reviews/           # 红方评审（按轮次 r01→r11）
```

> 阶段编号说明：第三阶段「上板 bring-up」以 `stage3-bringup/` 目录记录（点灯、STM32 ETM 自验、千兆网口链路三道单元门）。第四阶段把这些孤岛连成数据流，见 `PLAN_STAGE4.md`。

---

## Trace 引脚接线（STM32F429 → A7-Lite GPIO1）

STM32F429 的 4-bit 并口 ETM trace（TPIU）引脚全部集中在端口 E（PE2~PE6），接到 A7-Lite 的 **GPIO1 排针**，再由排针连到 XC7A35T 的 BANK16 输入引脚。

映射三方对齐来源：
- **FPGA 引脚**：工程约束 `syn/artix7/bringup/rtl/trace_mmcm.xdc`（`trace_clk_in` / `trace_data_in[3:0]`）
- **排针↔FPGA 引脚**：官方 `A7_LITE_GPIO.xlsx`（GPIO1 sheet）
- **STM32 引脚**：ETM3.5 TPIU 固定复用（PE2=TRACECLK，PE3~PE6=TRACED0~3）

| STM32 引脚 | 信号 | GPIO1 排针脚位 | 排针信号名 | FPGA 引脚 | I/O 标准 | 备注 |
|-----------|----------|:---:|-----------|:---:|-----------|------|
| PE2 | TRACECLK | 9 | GPIO1_4P | **D17** | LVCMOS33 | MRCC 时钟专用脚，进 MMCM |
| PE3 | TRACED0  | 1 | GPIO1_0P | F13 | LVCMOS33 | 2-bit 模式必接 |
| PE4 | TRACED1  | 4 | GPIO1_1N | E14 | LVCMOS33 | 2-bit 模式必接 |
| PE5 | TRACED2  | 5 | GPIO1_2P | D14 | LVCMOS33 | 仅 4-bit 模式 |
| PE6 | TRACED3  | 7 | GPIO1_3P | E16 | LVCMOS33 | 仅 4-bit 模式 |
| GND | 共地 | 12 或 30 | GND | — | — | 必须共地 |

> **2-bit（产品目标形态）**：只需接 PE2/PE3/PE4 → 排针脚 9/1/4（TRACECLK + TRACED0/1），IO 更省。
> **4-bit（交叉验证用）**：再补 PE5/PE6 → 排针脚 5/7。
> TRACECLK 落在 MRCC 引脚 D17 上，是为了让采样 MMCM 的专用时钟布线成立（见 `trace_mmcm.xdc` 注释）。

### 接线图

```mermaid
flowchart LR
    subgraph STM32["STM32F429I-DISC1 (ETM3.5 / TPIU)"]
        direction TB
        S_CLK["PE2 · TRACECLK"]
        S_D0["PE3 · TRACED0"]
        S_D1["PE4 · TRACED1"]
        S_D2["PE5 · TRACED2"]
        S_D3["PE6 · TRACED3"]
        S_GND["GND"]
    end

    subgraph HDR["A7-Lite GPIO1 排针"]
        direction TB
        H9["脚9 · GPIO1_4P"]
        H1["脚1 · GPIO1_0P"]
        H4["脚4 · GPIO1_1N"]
        H5["脚5 · GPIO1_2P"]
        H7["脚7 · GPIO1_3P"]
        HG["脚12/30 · GND"]
    end

    subgraph FPGA["XC7A35T BANK16 (trace_mmcm.xdc)"]
        direction TB
        F_CLK["D17<br/>trace_clk_in (MRCC)"]
        F_D0["F13<br/>trace_data_in[0]"]
        F_D1["E14<br/>trace_data_in[1]"]
        F_D2["D14<br/>trace_data_in[2]"]
        F_D3["E16<br/>trace_data_in[3]"]
        F_GND["GND"]
    end

    S_CLK ==>|"时钟 (2/4-bit)"| H9 ==> F_CLK
    S_D0 ==>|"数据 (2/4-bit)"| H1 ==> F_D0
    S_D1 ==>|"数据 (2/4-bit)"| H4 ==> F_D1
    S_D2 -.->|"数据 (仅4-bit)"| H5 -.-> F_D2
    S_D3 -.->|"数据 (仅4-bit)"| H7 -.-> F_D3
    S_GND --- HG --- F_GND

    classDef clk fill:#ffe6cc,stroke:#d79b00;
    classDef dat fill:#d5e8d4,stroke:#82b366;
    classDef opt fill:#f5f5f5,stroke:#999,stroke-dasharray:4 3;
    classDef gnd fill:#e1d5e7,stroke:#9673a6;
    class S_CLK,H9,F_CLK clk;
    class S_D0,S_D1,H1,H4,F_D0,F_D1 dat;
    class S_D2,S_D3,H5,H7,F_D2,F_D3 opt;
    class S_GND,HG,F_GND gnd;
```

> 图例：实线 = 2-bit / 4-bit 都需要；虚线 = 仅 4-bit 模式接。橙色=时钟，绿色=必接数据，灰虚=可选数据，紫色=地。

### SWO 单线 trace（支线，与并口互斥）

除并口 ETM trace 外，FPGA 还实现了 **SWO 单线** trace 前端（`swo_stream_top.v` + `swo_iddr_capture.v`，提案 15/17）。SWO 只用一根异步单线，无独立时钟——FPGA 用自由运行的 200MHz 参考时钟 + IDDR 双沿过采样（500 MSa/s）在 fabric 内解出 NRZ 字节流。SI 简单（杜邦线即可）但带宽受限（UART ≤12Mbaud≈1.2MB/s），用途见 `../swo-trace-sidetrack/`，可作满速并口的黄金对照基线。

| STM32 引脚 | 信号 | GPIO1 排针脚位 | 排针信号名 | FPGA 引脚 | I/O 标准 | 备注 |
|-----------|----------|:---:|-----------|:---:|-----------|------|
| PB3 | SWO / TRACESWO | 50 | GPIO1_21N | **B22** | LVCMOS33 | 异步单线，200MHz IDDR 过采样 |
| GND | 共地 | 12 或 30 | GND | — | — | 必须共地 |

> 引脚来源：`syn/artix7/bringup/rtl/swo_stream.xdc`（`swo_in`=B22）+ GPIO 表（脚50=GPIO1_21N=B22）。B22 是当初通过边界扫描 SAMPLE 探到用户实际插 SWO 线的脚位。

```mermaid
flowchart LR
    subgraph STM32B["STM32F429 (ITM/ETM over SWO)"]
        SB3["PB3 · SWO/TRACESWO"]
        SBG["GND"]
    end

    subgraph HDRB["A7-Lite GPIO1 排针"]
        HB50["脚50 · GPIO1_21N"]
        HBG["脚12/30 · GND"]
    end

    subgraph FPGAB["XC7A35T (swo_stream.xdc)"]
        FB["B22<br/>swo_in"]
        FBS["200MHz IDDR<br/>500MSa/s 过采样"]
        FBG["GND"]
    end

    SB3 ==>|"单线异步 (NRZ)"| HB50 ==> FB ==> FBS
    SBG --- HBG --- FBG

    classDef swo fill:#dae8fc,stroke:#6c8ebf;
    classDef gnd fill:#e1d5e7,stroke:#9673a6;
    class SB3,HB50,FB,FBS swo;
    class SBG,HBG,FBG gnd;
```

---

## 这是一场"红蓝对抗"式的选型推演

为避免一拍脑袋选型，本项目用 **蓝方（提方案）vs 红方（拆台质疑）** 的多轮对抗，把一个模糊想法逼成可落地的工程决策。文档按"蓝方提案 → 红方评审 → 蓝方修正"的节奏交替推进。

```mermaid
graph TD
    P00[00 ORBTrace软硬件分析<br/>作者有意闭源主板PCB] --> P01[01 软件层分析<br/>能否更少硬件实现]
    P01 --> R01{r01 精简可行性评审}
    R01 --> P03[03 Zynq正向方案]
    P02[02 Zynq-ETM生态调研] --> P03
    P03 --> R02{r02 Zynq评审}
    R02 --> P04[04 Zynq v2回应]
    P04 --> R03{r03 Zynq第二轮}
    R03 -->|DDR3缓冲类别错误<br/>对F4/F7过度设计| P06[06 iCESugar抓快照]
    P06 --> R04{r04 第四轮}
    R04 -->|环形缓冲冲掉UAF根因| P07[07 iCESugar v2降格]
    P07 --> R05{r05 第五轮}
    R05 -->|profiling无时间数据| P08[08 双轨方案v3]
    P08 --> R06{r06 第六轮}
    R06 -->|A轨非必需<br/>仿真更优| P09[09 博弈复盘最终结论]
    P09 --> R07{r07 终审确认}
    R07 -->|确认: PC+仿真+单块Artix| PLAN[PLAN 第一阶段计划]
    P10[10 逻辑门开销分析] --> R08{r08 开销评审}
    R08 -->|35T中位够 上沿贴边<br/>先跑OOC再定35T/100T| PLAN

    style P09 fill:#fff3cd
    style R07 fill:#d6ffd6
    style PLAN fill:#d6ffd6
```

---

## 最终结论（六轮收敛）

**采用方案丙**：PC+调试器验证 ETM 配置/解码链路 + 仿真 testbench 验证逻辑 + 只买一块 Artix-7+FT601/千兆网。**先做无硬件仿真验证（见 `PLAN.md`），暂不折腾板子。**

被否方案与核心原因：

| 方案 | 否决原因 |
|------|---------|
| 自制 ORBTrace PCB | 软硬件双未知；主板 KiCad 未开源 |
| RK3506 + DSMC | DSMC 无公开带宽/时序数据，不可立项 |
| Zynq 正向开发 | 对 F4/F7 过度设计；DDR3 缓冲是类别错误（抗失速缓冲须在 DMA 上游） |
| iCESugar 抓快照 | 环形缓冲冲掉 UAF 根因；串口导出分钟级；profiling 无时间数据 |
| 双轨并行 | A 轨非必需（能隔离的不需 FPGA / 仿真更优 / 对满速零证明力） |

**唯一不变的命门**：上板后的满速源同步采样 + 信号完整性，必须单独立 PoC，用眼图/时序硬阈值判收，仿真绿不为其背书。

---

## proposals/（蓝方方案）

| 文件 | 内容 |
|------|------|
| `00-orbtrace-硬件软件分析报告.md` | ORBTrace 软硬件分析、作者开源边界、可移植性 |
| `01-软件层分析与精简硬件可行性.md` | gateware 数据通路分析、能否用更少硬件实现 |
| `02-zynq-etm生态调研与定位.md` | 有无人用 Zynq 做 ETM trace、能否替代 J-Trace |
| `03-zynq正向开发方案.md` | Zynq-7000 正向方案 |
| `04-zynq方案v2-蓝方回应.md` | 回应 Zynq 第二轮评审 |
| `05-精简硬件可行性v2-蓝方回应.md` | 回应精简可行性评审（含 RK3506） |
| `06-icesugarpro抓快照方案.md` | iCESugar-Pro 抓快照方案 |
| `07-icesugarpro方案v2.md` | iCESugar 降格正名（链路验证+轻量 profiling）|
| `08-双轨方案v3.md` | iCESugar + Artix 双轨并行 |
| `09-博弈复盘与最终结论.md` | ★ 六轮复盘与最终选型 |
| `10-逻辑门开销分析表.md` | LUT/FF/BRAM 开销估算（35T 是否够）|

## reviews/（红方评审）

| 文件 | 对应 |
|------|------|
| `r01-精简硬件可行性评审.md` | 评 01/05 |
| `r02-zynq正向方案评审.md` | 评 03 |
| `r03-zynq方案v2第二轮.md` | 评 04 |
| `r04-icesugarpro抓快照第四轮.md` | 评 06 |
| `r05-icesugarpro-v2第五轮.md` | 评 07 |
| `r06-双轨方案v3第六轮.md` | 评 08 |
| `r07-博弈复盘终审确认.md` | 评 09（终审）|
| `r08-逻辑门开销分析表评审.md` | 评 10 |

---

## 当前进度

- [x] 选型收敛（方案丙）
- [x] fork + remote 配置 + `artix7-port` 分支
- [x] 文档归档
- [x] **第一阶段：无硬件仿真验证** —— 逻辑层非平台风险已消化（`pytest tests/` 11 passed + iverilog 物理层组帧/重同步/真实数据，详见 `PLAN.md`），过程中修复 2 个真实缺陷
- [x] **第二阶段：选板与 OOC 综合** —— 以太网栈 OOC 综合定板、采样前端原型综合、真实时钟约束时序收敛（详见 `PLAN_STAGE2.md`）
- [x] **第三阶段：上板 bring-up** —— 三道单元门全通：JTAG 点灯、STM32 ETM 自验（示波器确认 4-bit trace）、千兆网口 RGMII 链路（UDP 双向环回实测）。踩坑记录见 `stage3-bringup/`
- [ ] **第四阶段：trace 数据通路端到端打通** —— 把三个孤岛连成 `trace 引脚→traceIF→OrbFlow→UDP→Orbuculum` 数据流，端到端解出真实执行流（详见 `PLAN_STAGE4.md`，验证阶梯 V0→V4）
- [ ] **第五阶段：满速 PoC** —— 升速逼满速源同步采样命门，眼图 / 丢包实测定工具能力边界

---

## 相关支线任务

- **SWO 单线 trace 能力边界探索**：[`../swo-trace-sidetrack/`](../swo-trace-sidetrack/README.md)
  - 在 STM32F429 上实测 ITM/ETM-over-SWO 的可行性与边界，为本主线"为何必须并口高速 trace"提供实测依据。
  - 关键结论：SWO 单线 SI 简单（杜邦线即可）但带宽受限（UART ≤12Mbaud≈1.2MB/s）、M4 ETM 无地址过滤、ETM+ITM 不能经 SWO 混流；满速实时 + 多源时间戳对齐必须走并口 trace。
  - 副产物：可复现的 SWO/ETM/ITM 解码链路（CH343P + orbuculum/orbmortem），**可作为 Stage5 满速 PoC 的黄金对照基线**（同固件下用 SWO 解出的指令流校验并口数据通路）。
  - 工具补丁：orbuculum 的 CH343/稀疏同步适配已提交 fork 分支 `feature/ch343-swo-sparse-sync`。
