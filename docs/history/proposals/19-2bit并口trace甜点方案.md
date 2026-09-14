# 提案 19：2-bit 并口 trace —— SWO 与 4-bit 之间的 SI/带宽甜点

> 目标：评估用 **2-bit 并口 trace（TRACEDATA[1:0] + TRACECLK）** 作为 STM32F429
> 指令 trace 的采集前端，定位它在「SWO 单线」与「4-bit 满速」之间的工程甜点：
> **比 SWO 单线带宽高一截、能不开 stall 跑（接近）满速调用栈，又比 4-bit 并口 SI 简单
> 一半**。
>
> 一句话结论（实测锚定）：**对"只要函数级调用栈、不要数据 trace"的目标，2-bit 并口在
> 带宽上对满速 ETM 留有余量，SI 复杂度只有 4-bit 的一半（2 根等长数据线 + 时钟，无
> 4-bit 的 nARMED 长线命门），是一个值得做 PoC 的中间档。但 ETM-M4 无地址过滤，
> P-header 执行流骨架砍不掉（占当前 capture 的 89%），"只抓 BL/BLX/POP"无法再省一个
> 量级——这一点必须在期望管理上说清。**

---

## 0. 在 stage 框架中的定位

这**不是新 stage**，而是 **Stage 5（满速 PoC）下的一个采集前端子方向**。Stage 5 的命门是
"满速源同步采样 + 信号完整性"。本提案把这个命门**拆出一个低风险中间档**：先用 2-bit（SI
比 4-bit 简单）摸到"不开 stall 的接近满速调用栈"，作为 4-bit 满速的踏脚石与 SI 对照。

与既有工作的关系：
- **SWO 单线支线**（`swo-trace-sidetrack/`）：SI 最简、带宽最低（实测 56Mbaud≈45Mbit/s）。
- **本提案 2-bit 并口**：SI 中等、带宽中高。
- **Stage 5 4-bit 并口**：SI 最难（nARMED 长线命门）、带宽最高。

---

## 1. 实测锚点（不空算）

数据全部来自当前已打通的 SWO→FPGA→orbetto 链路（proposal 18 §9，proj_add 紧密循环固件，
stall 模式，2 Mbaud SWO，FPGA 时间戳）：

| 量 | 实测值 | 来源 |
|----|--------|------|
| ETM 字节 / 时间跨度 | 91196 B / 491.5 ms | swo_dump_banked + FPGA timebase |
| **stall 模式 ETM 码率** | **1.48 Mbit/s** | 上两者相除 |
| 包构成：P-header | 8105 包（**89%**） | etm_pkt_census |
| 包构成：间接分支地址 | 950 包（**10%**，return/BLX/POP pc） | etm_pkt_census |
| 包构成：I-sync 锚点 | 152 包 | etm_pkt_census |
| 字节 / 间接分支 | 96 | 推算 |
| 编码密度 | **1.12 bit/指令**（0.89 指令/Kbit） | sidetrack §13.2 实测 |

**关键事实**：当前 br_out=0（只发间接分支地址）下，**地址包仅占 10%，P-header 执行流骨架占
89%**。下一节解释为什么这决定了"只抓调用栈"省不动。

---

## 2. 核心约束：ETM-M4 无地址过滤，"只抓 BL/BLX/POP"省不掉 P-header

用户设想"只抓 BL/BLX/POP、不关心函数内 B"。在 STM32F429（ETM-M4 / ETMv3.5）上**硬件做不到
按指令类型过滤**，实测依据（sidetrack §14，逐寄存器读写验证）：

- ETM-M4 **0 个地址比较器**、无 start/stop block（ETMACVR/ETMTSSCR 写 0 读回 0）。
- 能调的只有 `br_out`：
  - `br_out=1`：广播所有分支地址。
  - **`br_out=0`（当前配置）**：只发**间接分支**地址（POP pc / BX lr 返回、BLX 寄存器、
    函数指针）。**直接分支（BL 到固定地址、函数内 B）不发地址**，其 taken/not-taken 由
    **P-header 的 1-bit atom** 表达。
- **P-header 是执行流骨架，砍不掉**：它逐条记录"指令执行/不执行"，是从 ELF 重建 BL 目标
  与调用栈的必需信息。函数内 B 的 atom 混在同一条 atom 流里，**无法单独剔除**。

所以：
- "只要调用栈" → **br_out=0 已是 ETM-M4 的下限**，地址包已压到 10%。
- 想再砍一个量级 → 只能靠**地址范围过滤（ETM-M4 没有）**或 **DWT PC 采样（非完整流，
  另一条路）**。sidetrack §13.4 实测：br_out=0 对调用密集型只省 ~24%，**非数量级**。

> 结论：2-bit 的价值在**通道带宽与 SI**，不在"靠过滤把码率打下来"。期望要管理好。

---

## 3. 2-bit 并口 SI：比 4-bit 少一半，比 SWO 多一根时钟

| SI 维度 | SWO 单线 | **2-bit 并口** | 4-bit 并口 |
|---------|---------|---------------|-----------|
| 数据线 | 1（异步 NRZ） | **2 + TRACECLK** | 4 + TRACECLK |
| 源同步时钟 | 无 | 有（1 根 TRACECLK） | 有 |
| 通道间 skew | 不存在 | **仅 2 数据线，等长易控** | 4 线等长 + 难 |
| nARMED 长线命门 | 无 | **无（只用 D0/D1）** | **有**（nARMED 接到某条 trace 线，3× 长，sidetrack §17.3） |
| 控阻抗根数 | 1 | 3 | 5 |
| 变频失锁 | 有（NRZ 软 SI） | 无（有时钟线） | 无 |

**SI 排序：SWO 单线 < 2-bit 并口 < 4-bit 并口。** 2-bit 拿掉了 4-bit 最痛的两条数据线 +
nARMED 长线问题，只保留 D0/D1 + TRACECLK，等长约束从 5 根降到 3 根。

---

## 4. 带宽估算：2-bit 对满速调用栈留有余量

### 4.1 满速 ETM 码率（不开 stall，用实测编码密度外推）

F429@168MHz，编码密度 1.12 bit/指令：

| 指令吞吐假设 | ETM 码率 |
|--------------|----------|
| 保守 80M instr/s | ~90 Mbit/s |
| 中 120M instr/s | ~135 Mbit/s |
| 峰值 150M instr/s | ~169 Mbit/s |

（注：这是 br_out=0 全程 trace 的满速码率上界；实际 CPI>1、且 trace 含大量直线代码用
P-header 极省编码，真实均值通常落在保守~中之间。）

### 4.2 2-bit 通道带宽（⚠️ SDR/DDR 待实测确认）

STM32 TPIU TRACEDATA 的数据率取决于 TRACECLK 是 SDR 还是 DDR——**这是必须上板实测确认的
关键点，不武断**：

| TRACECLK | 2-bit SDR（单沿，2×clk） | 2-bit DDR（双沿，4×clk） |
|----------|--------------------------|--------------------------|
| 25 MHz | 50 Mbit/s | 100 Mbit/s |
| 50 MHz | 100 Mbit/s | 200 Mbit/s |
| 75 MHz | 150 Mbit/s | 300 Mbit/s |

> CoreSight TPIU 通常以 DDR 输出 TRACEDATA（TRACECLK 两沿都有效），但**F429 具体行为 +
> 我们 FPGA IDDR 采样能否在该 TRACECLK 收敛，必须 PoC 实测**。FPGA 侧已有 IDDR 双沿采样
> 经验（proposal 16/17，SWO 400MSa/s 实测时序收敛）。

### 4.3 对照判断

- **SWO 单线上限 45 Mbit/s** → 扛不住满速（90-169），所以 SWO 必须开 stall（拖慢 CPU）。
- **2-bit @ TRACECLK 50MHz**：SDR 100 / DDR 200 Mbit/s → **覆盖保守~中满速码率**，
  接近/达到"不开 stall 跑满速调用栈"。
- **2-bit 比 SWO 高 2-4×带宽，且有时钟线不怕变频**。

**判断**：2-bit 在带宽上对"满速调用栈"留有余量（尤其 DDR 或 TRACECLK 能上到 50-75MHz
时），同时 SI 只有 4-bit 的一半。这正是甜点。

---

## 5. 落地路径（PoC，复用现有资产）

1. **STM32 侧**：TPIU `SPPR=0`（并口模式）、port size=2（TPIU_CSPSR=0x2）、配 TRACECLK
   分频；ETM br_out=0 + 不开 stall（先开 stall 验证链路，再关 stall 测满速）。GPIO 配
   TRACECLK + TRACED0/D1（PE2/PE3/PE4 一类，需查 F429 trace 复用脚）。
2. **FPGA 侧**：复用 stage4 的 traceIF 思路（4-bit 版已有），裁成 2-bit 输入；TRACECLK 做
   源同步采样（IDDR 若 DDR）。复用现有 BRAM 抓取 + UDP 读出 + **FPGA timebase（proposal
   18 §9.3 已实现）**。
3. **解码**：2-bit 流去帧后同样是 TPIU formatter 字节 → 现有 etm35lib / orbetto 链路直接
   复用（与 SWO 路解码同源）。
4. **判收**：与 SWO 黄金对照基线（同固件 SWO 解出的指令流）逐函数比对；眼图/时序硬阈值。

工作量：中。最大不确定性 = **TRACECLK SDR/DDR + 该频率下 FPGA 采样 SI 是否收敛**（§4.2），
这正是要 PoC 回答的。

---

## 6. 风险与诚实边界

| 风险 | 说明 | 缓解 |
|------|------|------|
| SDR/DDR 未定 | 带宽差 2×，结论区间敞口 | §5 PoC 第一步就示波器实测 TRACECLK 沿与数据关系 |
| P-header 砍不掉 | "只抓调用栈"省不掉 89% 骨架 | 期望管理；2-bit 价值在带宽/SI 不在过滤 |
| 满速 SI 命门 | 2-bit 仍是源同步采样，TRACECLK 越高越紧 | 先低频收敛再升频；眼图判收（同 stage5 命门，但只 2 线） |
| TRACECLK 占 GPIO | F429 trace 脚复用，需确认不与现用外设冲突 | 查 F429 数据手册 trace AF 表 |
| 满速码率上界仍可能超 2-bit | 峰值 169Mbit/s > 2-bit SDR@50M(100) | 若超，仍需开轻 stall 或升 TRACECLK；但已远胜 SWO |

---

## 7. 结论

- **2-bit 并口是 SWO 与 4-bit 之间的合理甜点**：带宽 2-4× 于 SWO、对满速调用栈留余量，
  SI 复杂度（3 根受控线、无 nARMED 长线）只有 4-bit 的一半。
- **"只抓 BL/BLX/POP"无法再省 P-header**（ETM-M4 无地址过滤，实测 §2）；2-bit 的收益来自
  通道而非过滤，期望要管理好。
- **建议作为 Stage 5 的第一个采集前端 PoC**：风险低于 4-bit，能先把"不开 stall 的接近满速
  调用栈"这件事做出来，并为 4-bit 满速积累源同步采样经验。
- **首个待实测问题**：F429 TRACECLK 的 SDR/DDR + 该频率下 FPGA 2-bit 采样的时序/眼图收敛
  （决定 §4 带宽结论落在哪一档）。
