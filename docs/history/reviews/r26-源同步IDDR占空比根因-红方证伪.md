# r26 — "2.5% = TRACECLK 占空比压窄 IDDR 下降沿窗口" 红方证伪

**日期**：2026-07-18  
**对象**：`stage4-datapath/16-source-sync-freq-ceiling.md` §"2.5% 的谜底：架构级根因 = TRACECLK 占空比 + IDDR 下降沿采样窗口"，及其依赖的 RTL `syn/artix7/rtl/trace_capture_a7.v` 的 `g_iddr` 分支（`{iddr_b,iddr_a}` 打包 + `trace_clk→ref_200m` 的 toggle CDC）。  
**立场**：严格证伪。  
**可复现资产（本次新增）**：`syn/artix7/bringup/sim/tb_iddr_cdc.v`、`tb_iddr_cdc_sweep.v`、`run_sweep.sh`（iverilog 12.0，已在本机跑通）。

---

## 一句话结论

> **"占空比"根因是一个从未被测量的假设。蓝方自己的 RTL 在 IDDR 分支把 duty 统计硬置 0（手上一个占空比数字都没有），却把"错误全在下降沿"直接扣到"占空比压窄窗口"上。而三条被当作正证的证据——错误值 100% 确定（0x5/0xa）、对 IDELAY tap 完全不敏感、跨频率平坦——恰恰是数字逻辑 bug（CDC 字节撕裂 / lane 配对）的指纹，是对占空比/模拟假设的反证。我用 iverilog 把 `g_iddr` 的 CDC 逐字节抄出来，喂理想 50% 占空比 + 干净 walking 数据，12 MHz 下误码 = 0.00%——占空比机制在零占空比失真下根本不需要就能既清白又出错，假设不成立。**

---

## R1 🟥 阻断 — 占空比从未被测量，IDDR 分支的 duty 统计硬置 0

**实测（RTL 阅读）**：`trace_capture_a7.v` 的 duty 测量逻辑（`duty_hi_sum/duty_lo_sum/duty_hi_cnt/...`）**只在 `g_oversample` 分支存在**。`g_iddr` 分支末尾：

```verilog
// IDDR mode does not measure duty; hold the stats at 0.
always @(posedge ref_200m) begin
    duty_hi_min <= 0; duty_hi_max <= 0; duty_lo_min <= 0;
    duty_lo_max <= 0; duty_hi_sum <= 0; duty_hi_cnt <= 0;
    duty_lo_sum <= 0; duty_lo_cnt <= 0; glitch_cnt <= 0;
end
```

而 §16 §方法学明确写本轮用 `CAP_METHOD=IDDR, CAP_RAW=1`。**结论是在 IDDR 路径下的，但 IDDR 路径下占空比恒读 0，蓝方手上没有任何一个 TRACECLK 实际占空比数字**。§"根因"却断言"STM32 出来经飞线+IBUF，占空比非精确 50%，fall 沿离 rise 沿更近"——**这是纯推断，零实测支撑**。

**判决**：把未测假设写成"锁死的根因"。**证据缺失即结论不成立。**

**零成本先测再说（root，全部现成）**：
1. **OVERSAMPLE 分支已经有 duty 统计**：用 `CAP_METHOD=OVERSAMPLE` 抓同一 CURTPM 图案，读回 `duty_hi_sum/cnt`、`duty_lo_sum/cnt`，直接算 `duty = hi_avg/(hi_avg+lo_avg)`。这块逻辑蓝方自己写了却没用来验证自己的根因。
2. **pin-LA 400 MSPS**：直接测 TRACECLK 高/低电平停留比。§方法学说 pin-LA 会混叠频率——但**占空比是同一次采样内高低样本数之比，不受绝对频率混叠影响**，仍可用。
3. **示波器**：TRACECLK 单通道，测 duty，5 分钟的事。
4. 判据：若实测 duty ∈ [45%, 55%]（12 MHz 半位 41.7 ns，即便 48/52 也有 40 ns 窗口，远大于 IDDR setup/hold），**"占空比压窄窗口"当场被证伪**。

---

## R2 🟥 阻断 — iverilog 理想输入复现：12 MHz 下 CDC 误码 = 0.00%，占空比机制不必要

**实测（仿真）**：`sim/tb_iddr_cdc.v` 把 `g_iddr` 的 CDC **逐字节 verbatim 抄出**（`tclk_byte/tclk_tgl` 发射 + `tgl_sync/byte_s0/byte_s1` 接收 + `cap_valid=tgl_sync[2]^tgl_sync[1]`、`cap_byte=byte_s1`），IDDR 原语用理想行为级双沿采样替代。输入：**精确 50% 占空比** trace_clk（`always #41667`，两个半周期完全相同）+ 干净 walking-1s 数据 + 200 ps 级 per-bit 发射 skew。

```
=== RESULT (BITSKEW=200 ps, ideal 50% duty, clean data) ===
captured bytes = 48
bad bytes      = 0  (0.00%)
  lo-nibble==0x5 : 0
  hi-nibble==0xa : 0
```

**12 MHz、零占空比失真下，这条 CDC 一个错都不出。** 那么蓝方在 12 MHz 实测的 2.5-3.9% 有两种可能：
- (a) 真是模拟效应 —— 但见 R3，模拟效应必随 tap 变，而蓝方说 tap 无关，自相矛盾；
- (b) 错误不在这条我抄出来的 CDC，而在别处（pin-LA 采集、nibble 打包顺序、Q1/Q2 配对、或实际 IDDR 原语的 `SAME_EDGE_PIPELINED` 流水延迟）——**那"占空比"更是无从谈起**。

无论哪种，**"占空比压窄下降沿窗口"都不是被证明的根因**。

**注**：12 MHz 时 ref_200m 每个 TRACECLK 半周期有 ~8 个样本、整周期 ~16 个，toggle CDC 有 8× 过采样余量，本就不该错——这也说明 §16 若在 12 MHz IDDR 下真有 2.5% 错，问题几乎不可能是"下降沿窗口被压窄到采不到"。

---

## R3 🟥 阻断 — "0x5/0xa 恒定 + tap 无关 + 跨频平坦" 是数字 bug 的正证，被蓝方当成占空比的正证

**实测（数值分析）**：
- `0x5 = 0x1 | 0x4`，`0xa = 0x2 | 0x8`。这是**两个 one-hot 值的按位 OR 叠加**。
- walking-1s 保证每个 nibble 严格 one-hot（只有一条 lane 高）。要在一个 fall nibble 里同时看到两条 lane 高（0x1 和 0x4 = lane0 和 lane2），只能是**两个不同时刻/周期的采样值被合并进了一个字节**。

**推断（强）**：模拟占空比失真会把采样点**移到相邻半位**，读到的是"错误但合法"的**单个**邻居 nibble 值（比如该读 0x1 读成 0x2），**不会**产生 `0x1|0x4` 这种两值 OR。CMOS 推挽驱动的两条独立 lane 在物理上也无法"线与"成 OR。**OR 叠加是数字位撕裂（byte tear）/ 配对错位 / 亚稳合并的签名，不是模拟边沿效应的签名。**

**更致命的 tap-无关论证（推断，强）**：IDELAY tap 移动的是**数据相对 trace_clk 的相位**。而 `g_iddr` 的字节撕裂发生在 **`trace_clk → ref_200m` 的 CDC 交接**（`tclk_byte` 在 trace_clk 域打包，`byte_s0/s1` 在 ref_200m 域取样）——**这一步的相位关系由 trace_clk 与 ref_200m 决定，IDELAY 完全够不着**。所以：

> **"对 tap 完全不敏感"恰恰是故障位于 IDELAY 下游（CDC 字节交接）的指纹**。蓝方把这条**指向数字 CDC 的最强证据**，反读成了"排除 SI ⇒ 所以是占空比"。这是**把反证当正证**。

**判决**：§"根因"第 3 条"对 IDELAY tap 完全不敏感 → 排除采样相位/眼图/SI/skew"逻辑只做了一半——排除了 IDELAY 能影响的模拟项，却没排除 **IDELAY 够不着的数字 CDC 项**，然后跳到"占空比"。**逻辑跳步。**

**辅证（仿真）**：`sim/tb_iddr_cdc_sweep.v` 在**纯数字、理想 50% 占空比**下扫频，注入 per-bit 发射 skew 模拟字节总线非原子交接：

```
TRACECLK=12.10MHz  bad=31.94%  0x5-tears=0  0xa-tears=12
TRACECLK=48.10MHz  bad=16.72%  0x5-tears=48 0xa-tears=0
TRACECLK=75.10MHz  bad=33.18%  0x5-tears=16 0xa-tears=75
TRACECLK=100.0MHz  bad= 0.00%  0x5-tears=0  0xa-tears=0
TRACECLK=125.0MHz  bad=19.91%  0x5-tears=75 0xa-tears=74
TRACECLK=150.2MHz  bad= 3.67%  0x5-tears=0  0xa-tears=16
TRACECLK=198.2MHz  bad=17.03%  0x5-tears=66 0xa-tears=68
```

一个**纯数字**机制就能产出**正是 0x5 / 0xa 的 OR 撕裂**，且随频率**非单调**（100 MHz 恰好 ref/2 整除相位稳定 → 0%）。这个"非单调/跨频不陡升"的行为，比"模拟带宽墙应随频率陡升"更贴合蓝方自己观测到的"~2.5% 跨频平坦"。（注：此 sweep 用人为 skew 建模，数值本身不代表真板，仅证明**数字机制足以复现该签名**——占空比不是必要条件。）

---

## R4 🟨 高风险 — CDC 数据链与选通链对齐未经审查，存在系统性差一拍风险

**实测（RTL 阅读）**：`g_iddr` 的接收侧

```verilog
always @(posedge ref_200m) begin
    tgl_sync <= {tgl_sync[1:0], tclk_tgl};   // 选通链：3 级
    byte_s0  <= tclk_byte;                    // 数据链：2 级
    byte_s1  <= byte_s0;
end
assign cap_valid = tgl_sync[2] ^ tgl_sync[1]; // 用第 2/3 级
assign cap_byte  = byte_s1;                    // 用第 2 级
```

- **选通** `cap_valid` 由 `tgl_sync[2]^tgl_sync[1]` 产生，对应 `tclk_tgl` 经过 **2~3 拍**同步后的边沿。
- **数据** `cap_byte=byte_s1` 是 `tclk_byte` 经过 **2 拍**。
- `tclk_byte` 和 `tclk_tgl` 在发射侧同一个 `posedge trace_clk` 更新，但 `tclk_byte` 是 8 bit 宽总线（布线延迟分散、非原子），`tclk_tgl` 是 1 bit（快）。**选通用 3 级链的异或、数据用 2 级链的直取，两条链对齐没有形式化证明**。

**推断**：在特定 trace_clk/ref_200m 相位下，`cap_valid` 打出的那一拍，`byte_s1` 可能刚好夹在"周期 k 的部分 bit 已更新、周期 k+1 的部分 bit 未到"的撕裂窗口 → 把相邻两周期的 fall/rise nibble 混进一个字节。这与 R3 的 OR 叠加现象**完全自洽**，且比"占空比"更直接。

**修复动作（root）**：
1. 数据链与选通链**对齐到同一级**：用 `byte_s1` 时选通应取 `tgl_sync[2]^tgl_sync[1]` 之后**再延一拍**，确保 `cap_valid` 有效时 `byte_s1` 已是**完全属于同一周期**的稳定值；或改用发射侧把 `{byte, tgl}` 作为一个原子字打进异步 FIFO（gray-code 指针），彻底消除总线非原子交接。
2. 或者在发射侧对 `tclk_byte` 做一次 trace_clk 域的**保持寄存**，保证 CDC 采样时数据已在 trace_clk 域稳定 ≥1 整周期（当前直接把组合更新的 `tclk_byte` 暴露给 CDC）。
3. 用 R2 的 testbench 把**真实 IDDR `SAME_EDGE_PIPELINED` 延迟**（Q1/Q2 相对 C 的流水一拍）加进去复跑——`SAME_EDGE_PIPELINED` 会让 Q1/Q2 都晚一个 trace_clk，若打包时序没跟上，就是确定性错位。

---

## R5 🟨 高风险 — 频率维度缺失：占空比假设的必然推论未被检验

**实测（文档）**：§"2.5% 的谜底"整节的 fall/rise 分离（fall 3.4-3.9% / rise 0.6%）**只在 12 MHz 测了一个点**。§前面 walking 扫频表（75→198 MHz ~2.5% 平坦）用的是**总误码**，没有 **fall 误码率 vs 频率**曲线。

**推断**：若"占空比压窄 fall 窗口"为真，fall 窗口的**绝对时间**随频率升高线性缩短（12 MHz 半位 41.7 ns → 198 MHz 半位 2.5 ns），fall 误码率**必随频率单调恶化**。蓝方没给这条曲线。

**判据（root）**：补测 fall 误码率 vs {12, 48, 75, 125, 198} MHz：
- 若 fall 误码率**随频率平坦** → "窗口压窄"被证伪（窗口都缩到 1/16 了误码还不变，说明与窗口无关）。
- 若**单调恶化** → 占空比假设获得第一个真实支撑（但仍需 R1 的 duty 实测 + R3 的 OR-签名解释）。

---

## R6 🟨 高风险 — rise 也有 0.6% 错，占空比模型无法解释，指向第二机制

**实测（文档）**：§决定性证据链第 1 条 "RISE nibble 只 0.6%"。

**推断**：如果机制纯粹是"占空比把 fall 半位压窄、rise 半位被拉长"，那 rise 半位**变宽**、采样更从容，rise 误码应趋近 0，而不是 0.6%。0.6% 是一个**与边沿方向无关的本底**，指向第二个机制（更可能就是 R3/R4 的 CDC 撕裂本底，对 rise/fall 都作用，只是 fall 叠加了别的）。蓝方用单一"占空比"机制解释了 fall，却把 rise 的 0.6% 晾在一边。

**判据**：解释 rise 0.6% 的来源；若与 fall 错误同源（CDC），则占空比模型多余。

---

## R7 🟨 高风险 — 解法直接跳到 MMCM 相移 / ISERDES，未先排除数字 bug（成本倒挂）

**实测（文档）**：§解法列了 (1) 移数据 1/4 UI、(2) **MMCM 相移采样时钟**、(3) **ISERDES 过采样**——全是**大改采集架构**的方案。

**推断**：在 R1（测 duty）、R2/R3/R4（iverilog 复现 + 修 CDC 对齐）这些**零成本/一天工作量**的数字排查做完之前，就投入 MMCM 锁相（还带来"gap 失锁"与"停走时钟"需求冲突，蓝方自己在 §解法 2 承认）或 ISERDES（"工程量最大"）——**成本与证据严重倒挂**。若根因其实是 R4 的差一拍，MMCM/ISERDES 改完**错误照旧**（因为 CDC 交接 bug 与采样时钟相位无关）。

**修复顺序（root，强制）**：
1. R1 测真实 duty（现成三法任一）。
2. R2 iverilog 加真实 IDDR 流水 + 真实相位复跑，定位 tear 是否来自 CDC。
3. R4 修 CDC 对齐（异步 FIFO / gray 指针 / 数据链选通链对齐），重测 walking。
4. **只有** duty 实测确认失真 **且** CDC 修好后 fall 误码仍在，才谈 MMCM/ISERDES。

---

## 结论表：实测 vs 推断

| # | 结论 | 标签 | 关键证据 |
|---|------|------|----------|
| R1 | IDDR 路径下 duty 从未测量（RTL 硬置 0） | **实测** | `trace_capture_a7.v` g_iddr 分支 duty 全 `<= 0` |
| R2 | 理想 50% 占空比 + 干净数据，12 MHz CDC 误码 0.00% | **实测(仿真)** | `sim/tb_iddr_cdc.v` 运行结果 |
| R3 | 0x5/0xa 是两值按位 OR，属数字撕裂签名；tap 无关 = 故障在 IDELAY 下游 CDC | **推断(强) + 实测(sweep 佐证)** | `0x5=0x1\|0x4`；CDC 在 IDELAY 下游；`tb_iddr_cdc_sweep.v` |
| R4 | 数据链(2级)/选通链(3级)对齐未证明，存在差一拍撕裂风险 | **实测(RTL) + 推断** | `tgl_sync[2]^[1]` vs `byte_s1` |
| R5 | fall 误码 vs 频率曲线缺失（占空比必然推论未验） | **实测(缺口)** | §只测 12 MHz 一点 |
| R6 | rise 0.6% 本底占空比模型无法解释 | **实测 + 推断** | §证据链第 1 条 |
| R7 | 未排除数字 bug 就跳 MMCM/ISERDES，成本倒挂 | **推断** | §解法 1/2/3 |

---

## 总评

### Verdict：**根因未成立，"占空比"结论必须撤回或降级为"未验证假设"**

- 占空比这个**唯一被写成"锁死根因"的机制，恰恰是唯一一个连一个实测数字都没有的**（R1）。
- 蓝方引以为据的三条"决定性证据"里，**tap-无关（R3）是数字 CDC 的正证、反而是占空比的反证**；**错误值恒定 0x5/0xa（R3）是位撕裂的签名，不是模拟边沿的签名**；**跨频平坦（R5/R6）是与频率无关机制的特征，与"窗口随频率压窄"矛盾**。三条全被读反了。
- iverilog verbatim 复现（R2）证明：零占空比失真下这条 CDC 能干净——**占空比不是复现 2.5% 的必要条件**。

### Next-step 优先级

- **P0**（不做不能定根因）：
  - R1 用现成 OVERSAMPLE duty 统计 / pin-LA / 示波器**实测 TRACECLK 占空比**
  - R2+R4 iverilog 加真实 IDDR 流水与相位，复现 tear；修 CDC 数据/选通对齐（异步 FIFO 原子交接）
- **P1**：
  - R5 补 fall 误码率 vs 频率曲线（证伪/证实"窗口压窄"）
  - R6 定位 rise 0.6% 第二机制
- **P2**（仅当 P0/P1 确认真有模拟占空比失真且 CDC 已清白）：
  - R7 才考虑 MMCM 相移 / ISERDES

### 一句话给用户

**"占空比压窄下降沿窗口"是把一个没测过的模拟故事，套在一堆本该指向数字 CDC bug 的证据上——错误值恒为 0x5=0x1|0x4 是两条 lane 的按位 OR（位撕裂签名，不是边沿抖动），"对 tap 完全不敏感"正说明故障在 IDELAY 够不着的 trace_clk→ref_200m 字节交接里；我把这段 CDC 原样喂 iverilog、给它完美 50% 占空比，12 MHz 下零误码。先花五分钟量一下真实占空比、把 CDC 的数据链选通链对齐修了，再谈 MMCM/ISERDES 这种大改。**
