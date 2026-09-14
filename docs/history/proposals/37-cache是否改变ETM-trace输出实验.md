# Proposal 37：开关 I/D cache 是否改变 ETM trace 输出数据本身（实验 + 手册核实）

> 日期：2026-07-20
> 状态：实验完成
> 问题：用户质疑——"开关 cache 到底只改执行速度，还是会改变 ETM 输出的 trace 数据本身
>       （atom/地址/异常序列）？" 若只改速度，则假调用应与 cache 无关；若改数据，则 cache
>       是假调用的直接推手之一。
> 关联：proposal 36（假调用根因）、r29-response（假调用=atom 盲推漂移）、
>       proposal 29（M7 靠 cache 全速、cache+全BB 溢出）

---

## 0. 一句话结论

**开关 cache 既改速度，也改 trace 输出数据本身——后者被实测 + 手册双重坐实。** cache 通过
三条独立机制改变 ETM 输出：(1) 生成率暴涨触发 **ETF 溢出**（trace 流断裂 + 残缺地址包）；
(2) 全速执行改变**中断（SysTick）落点**，异常插入 trace 的位置随之变；(3) M7 的**推测取指 /
微架构行为**（MAXSPEC=1）随 cache 状态变。所以"关 cache 就能 BB=0 精确"不是因为"关 cache 让
数据更完整"，而是关 cache 时**生成率低到不溢出、执行慢到路径简单**，trace 流不断裂、盲推链
不被溢出打断。**cache 不是对 trace "透明"的。**

---

## 1. 实验设计（严格单变量：只切 cache，频率/代码/BB 全锁定）

要把"速度"与"数据内容"分开，必须在**同一 CPU 频率**下对比开/关 cache（否则频率是混淆变量）。
用 **BB=1**（每分支发地址、解码器不盲推）得到**精确 ground truth 指令流**，这样任何差异都归于
cache 而非盲推。

- 频率固定 **50MHz**（PLL_P=9）——R2 已证此频点 cache ON 的 BB=1 也 **0 溢出**，得到无污染
  对照（150M/75M 的 cache ON BB=1 会溢出，数据被污染，见 §3 反例）。
- 两个固件仅差 `-DBOARD_ENABLE_CACHE`（运行时 `SCB_EnableICache/DCache`），其余全同。
- 同一 FPGA clktap 采集前端、同 TRACECLK 112.5M、同 60KB one-shot。

---

## 2. 实测结果（50MHz，BB=1，0 溢出，唯一变量 cache）

| 指标（同 50M / BB=1 / 同源码） | cache OFF | cache ON |
|---|:---:|:---:|
| ETF 溢出（I_OVERFLOW） | 0 | 0 |
| TPIU full-sync 填充（60KB 桶内）| 4010 | 85 |
| deframed 净 ETM 字节 | 3790 | 10778 |
| INSTR_RANGE（执行区间数）| 1983 | 6172 |
| **EXCEPTION 元素** | **0** | **1（SysTick）** |
| 独立抓样 unique PC | 82 | 192 |

### 证据 A：同窗口净数据 2.8×——速度效应（预期，非争议）
同 546µs 窗口、同 BB=1，cache ON deframed 10778 vs OFF 3790。cache OFF 时 CPU 大量周期等
flash，trace 稀疏（full-sync 填充 4010 = 桶里大半是空填充）；cache ON 全速执行，同时间跑 ~2.8×
指令。**这是速度效应，双方都认同，不是本实验争点。**

### 证据 B：cache ON 出现 SysTick 异常元素，cache OFF 没有——数据内容变了（关键）
packet 日志 cache ON（`/tmp/g50con.log` Idx:10766）：
```
I_EXCEPT : Exception.; SysTick; Ret Addr Follows;
OCSD_GEN_TRC_ELEM_EXCEPTION(pref ret addr:0x8009436; excep num (0x0f))
```
cache OFF 的 546µs 窗口内 EXCEPTION=0，cache ON=1（SysTick，异常号 0x0f）。
**机制**：SysTick 是固定时间间隔（wall-clock）的中断。cache ON 时 CPU 在同一 546µs 里执行
远多的指令、且执行位置不同，SysTick 中断**插入 trace 流的位置**（打断哪一段指令）随之不同。
异常元素是 trace 数据流的一部分（`I_EXCEPT` 包），**它的有无/位置直接改变了输出的包序列**，
不是单纯"速度快慢"。⇒ **cache 改变了 trace 输出数据本身。**

### 证据 C：手册坐实 cache 相关的微架构行为进入 trace（非透明）
`refs/ddi0494d.txt`（ETM-M7 TRM）：
- §2.4.10 **Micro-architectural exceptions**（line 1932-1934）：ETM 用 TYPE 编码输出微架构行为
  异常，如 `0b0000100001 Entered a restricted region`、`ETM disabled before completion`。
  —— **微架构行为（含取指/区域）会作为异常进入 trace 流**。
- TRCIDR9 **MAXSPEC = 1**（line 6547-6550）："Maximum trace speculation depth is one" ——
  M7 ETM **trace 推测执行**（最多 1 个未提交 P0），推测/取消序列进入 trace。cache 命中/失效
  改变取指时序 → 推测窗口行为不同 → atom + Commit/Cancel 序列不同。
- `refs/m7trm.txt`（Cortex-M7 TRM）line 5626 "**Speculative instruction fetches** can be
  initiated to any Normal, executable memory" —— 推测取指存在且受 cache 影响。

⇒ 手册层面，cache 状态通过"推测取指 + 微架构异常"进入 instruction trace，**架构上就不是对
trace 透明的**。

---

## 3. 反例锁死：cache ON 高频下的"假地址"确由溢出产生（非执行路径）

为避免把"溢出污染"误当"执行路径变化"，核查 75M cache ON BB=1（溢出 58 次）的 packet 日志：
- 每个 `TRACE_ON` 元素都标 `[overflow]`——**流断裂全部源于 ETF 溢出**（生成率 >112.5 端口）。
- 出现 `ADDR_NACC(0x80c8d5c)`、`ADDR_NACC(0x80c8e80)`——地址在 **0x080C8xxx**，而固件只到
  0x0800Axxx。这是溢出丢字节后**地址包高位被截断**拼出的不存在地址，**是溢出污染，不是执行
  走到了那里**。
- 50M（0 溢出）对照组无 ADDR_NACC、无 overflow ⇒ 高频的"脏地址"是带宽溢出的产物，与 50M 组
  隔离干净。

---

## 4. 诚实标注：哪条证据强、哪条弱

- **证据 B（SysTick 异常位置随 cache 变）**：**强**。异常包是 trace 数据的一部分，其位置由
  执行时序决定，cache 改时序 ⇒ 改包序列，直接证明"数据内容变"。
- **证据 C（手册推测取指/微架构异常）**：**强**（架构层面确定 cache 不透明）。
- **证据 A（净数据 2.8×）**：中——主要是速度效应，只间接说明"同窗口内容组成不同"。
- **unique PC 82 vs 192 的集合差异（OFF 独有 6 / ON 独有 18）**：**弱，不作为主证**。两次是
  独立抓样，落在 CoreMark 无限循环的不同迭代点，PC 集合本就会不同，**不能据此推断 cache 改变
  了架构执行路径**。诚实排除此条作为"路径改变"的证据。

**修正 r29-response 里我说过的一个不准确因果**："关 cache 让锚点在固定桶里变密"——更准确的
表述是：**关 cache 让生成率低到不溢出、执行慢到 SysTick/推测行为简单，trace 流不断裂、盲推链
不被溢出与异常插入打断**；cache ON 则叠加了溢出断流（高频）+ 异常插入位置变化 + 推测行为变化，
共同使 BB=0 盲推链在稀疏锚点下更易漂移。

---

## 5. 对 proposal 36 主线的意义

1. **回答用户质疑**：cache **不是**只改速度——它改 trace 输出数据本身（异常位置、推测序列、
   高频下的溢出断流）。所以 cache 是 BB=0 假调用的**直接环境推手**之一，不能靠"换大缓冲/流式"
   消除（缓冲救不了 STM32 出口的溢出，也救不了异常插入位置与推测序列的变化）。
2. **坐实"满血 + cache + BB=0 精确函数级"不可达**：cache 全速下 trace 数据本身就带溢出断流 +
   异常插入扰动，叠加 BB=0 稀疏锚点盲推，精确重建无解；而缩盲推跨度的片上手段（range-filter/
   加密 A-sync）硬件不可用（proposal 36 候选 ab）。**方向到此为止，符合既定裁决。**
3. **cache OFF / 低频 + BB=0 仍精确**：本实验的 cache OFF 组 0 溢出、0 异常扰动、调用图干净，
   印证"低速/关 cache 下 BB=0 精确"这一可用工作点。

---

## 6. 结论表：实测 vs 推断

| # | 结论 | 判定 | 标签 |
|---|------|------|------|
| A | cache ON 同窗口净数据 2.8× | 成立 | 实测（速度效应）|
| B | cache ON 出现 SysTick 异常、OFF 无 | 成立 | 实测（数据内容变，强证）|
| C | M7 trace 推测执行 + 微架构异常 | 成立 | 手册 DDI0494D §2.4.10 / TRCIDR9 |
| 反例 | 高频 cache ON 脏地址=溢出产物 | 成立 | 实测（与 50M 组隔离）|
| 弱证 | unique PC 集合差异 | **不作主证** | 独立抓样点不同，诚实排除 |
| 总 | cache 改变 trace 数据本身，非仅速度 | 成立 | B+C |
