# r11 · Stage-2 收敛终审（第三轮，收敛判断 + 可穷举硬门）

> 评审对象：`proposals/13-r10回应-独立证据交付.md` + `proposals/12-deskew方案选型.md` + 实际 RTL/sim/tcl
> 已独立核对：`trace_probe_top.v`（L180-320 实读）、`sim/frame_cdc_tb.v`（全文实读）、`check_bank.tcl`（存在）、`run_capture_bufr.tcl`（**搜索：不存在**）
> 立场：红方第三轮。**本轮必须收敛**——要么 GO，要么给出**可穷举、可完成**的最后硬门，不允许开放式挑刺导致第四轮。

---

## 总体结论 + 收敛判断

**NO-GO（暂不下单），但已实质收敛。剩余阻塞收敛到 4 个工程硬门 + 1 个外部依赖，全部可穷举、可在买板前完成。补完这 4 个硬门即 GO——红方在此明确承诺：不再开新的工程必补项。**

收敛理由：r09→r10→r11 三轮，前两轮的 P0（pipeline 被剪、多 bit 裸跨域、无反压丢帧）这一轮**确实被实证修复**了——`frame_cdc_tb.v` 是真东西（我逐行读了），不等比时钟 + 抖动 + 200 帧 + 账目平（sent=received+lost），这是 r09/r10 一路要的"结构对 + 行为对"的正面闭环。蓝方的调试记录（5 次 FAIL 各抓一个行为 bug）也佐证了 CDC 是真验过、不是走过场。

但本轮发现**蓝方有 2 项"声称交付、实际是占位符/未跑"**（N3 的 `run_capture_bufr.tcl` 文件不存在；A1 端到端蓝方自己承认路径为空、降级成分段且缺 clk100 段），这些是**纯综合/仿真就能在买板前关闭**的，不是硬件风险，所以必须补完再下单。

**预期**：这是最后一轮工程拉锯。补完下面 4 个硬门，红方 GO。剩余的（PHY strap 实际值、上板眼图窗口、亚稳态 MTBF）是**真实硬件风险，接受带入 Stage-3**。

---

## 收敛清单速览

| 项 | 类别 | 状态 | 买板前能关？ |
|---|---|---|---|
| NEW-1 无反压丢帧 | 工程 | ✅ 真闭环（counter + 账目平实证） | — |
| B2 CDC 双时钟回归 | 工程 | ✅ 闭环为**功能正确性**（非亚稳态） | — |
| sys_rst 跨域 | 工程 | ✅ 闭环（FIFO 内部 sync_reset 已举证） | — |
| D1 bank | 工程 | ✅ 真闭环（get_property + 抓出 xlsx 错） | — |
| C2 deskew 选型 | 设计 | ✅ 闭环（方案3 定了） | — |
| **HG-1 A1 clk100 段连通** | 工程 | 🟥 缺证据（端到端空，分段只给了 2/3 段） | ✅ 是 |
| **HG-2 N3 BUFR 敏感性** | 工程 | 🟥 **文件不存在，未跑** | ✅ 是 |
| **HG-3 RGMII RX IDELAY 双轨综合** | 工程 | 🟥 只留 parameter，=1 未综合 | ✅ 是 |
| **HG-4 D2 UG475 自查** | 工程 | 🟥 未做（可不等客服自查） | ✅ 是 |
| P0-1 PHY strap 实际值 | 外部 | ⏸ 接受带入 Stage-3（MDIO 兜底接口已在） | ❌ 需板/客服 |
| 上板眼图 ≥8 tap 窗口 | 硬件 | ⏸ 接受带入 Stage-3 | ❌ 需上板 |
| 亚稳态 MTBF | 硬件 | ⏸ 接受带入 Stage-3（靠成熟核 + 上板） | ❌ 需上板 |

---


## A 项：已闭环项的证据强度复核

### A1-r11〔frame_cdc 双时钟回归〕✅ 闭环为「功能正确性」，要求划清边界

**红方独立读了 `frame_cdc_tb.v` 全文**，确认它真做了蓝方声称的事：
- `trace_clk` 半周期 `5.0 + jit`，`clk100` 半周期 `5.15 + jit` → **不等比（10.0 vs 10.3ns）+ LCG 抖动**，真的；
- consumer `throttle % 3 == 0`（1/3 ready）故意灌满 FIFO，真的；
- 三条不变量：payload 不损坏 / 单调不乱序 / `sent == received + trace_lost_cnt`，真的；
- `accepted != received` 也单独断言（FIFO 内部零丢失），真的。

**这是扎实的功能回归，认可闭环。** 这是三轮里最实的一块交付。

**但红方要求蓝方在文档里划清边界（不许夸大）**——红方主动替蓝方把话说全，避免后续误读：

| frame_cdc_tb 证明了 | frame_cdc_tb **没**证明 |
|---|---|
| 跨域无逻辑丢帧（账目平） | 亚稳态安全性（iverilog 是 2-state，不传播 X） |
| 无数据损坏 / 无乱序 | 真实硅片 Gray 指针跨域 MTBF |
| overflow 被 counter 全捕获 | 一种时钟比；非全部 trace_clk 频率 |

**亚稳态安全性依赖的是 `axis_async_fifo` 本身的 Gray 码设计（Alex Forencich 成熟核）+ 上板 MTBF**，不是这个 tb 能证的。**红方认可这个分工**——不要求蓝方证明亚稳态（iverilog 做不到，这是合理的工具边界），但要求文档明写"本回归=功能正确性，亚稳态靠成熟核 + 上板"。

**补充覆盖率要求（并入 HG-1 之外的"建议"，不作硬门）**：tb 只测了 trace_clk≈clk100 近似同频。红方原想要求补 trace_clk 明显快于 clk100（6ns vs 10ns，模拟 168MHz trace）的场景——但**考虑收敛**，这一条降级为"建议"不作硬门：理由是 ① throttle 1/3 已经把 FIFO 逼到溢出 17 帧，FIFO-full 路径已被覆盖（快 trace_clk 无非是更容易溢出，溢出路径已验）；② 真实 STM32F429 TRACECLK ≤100MHz，6ns（168MHz）场景当前用不到。**写进 Stage-3 must-do 即可。**

**判定**：✅ **闭环**（功能正确性）。要求文档加边界声明，不作阻塞。

### A2-r11〔trace_lost_cnt 可观测性〕🟥 但降级为 Stage-3 设计项，不阻塞下单

**红方实读 RTL 确认**：`assign trace_dbg_lost = |trace_lost_cnt;`——**确实只是一个 sticky OR bit**（"曾经溢出过"），上板只能看到"丢过帧"，看不到丢了多少、何时丢。

**红方判定**：质疑成立——一个 GPIO sticky bit 对 PC 解码无用。真实用途需要把 `trace_lost_cnt`（16 bit）随 trace 流进 OrbFlow，对标 upstream `util.Monitor.lost` 进协议（orbtrace 在 overflow 时往流里插一个 overflow packet，解码端在时间轴标注）。

**但这是 Stage-3 的数据通路设计**（lost 计数怎么进 OrbFlow），**不是买板前必须关的**——Stage-2 的目的是"量化丢帧问题存在且可计数"，这点 `trace_lost_cnt` 已达成。**降级为 Stage-3 must-do，不阻塞下单。** 要求蓝方在文档写明"Stage-3 需把 trace_lost_cnt 纳入 OrbFlow，对标 Monitor.lost，而非停在调试 LED"。

**判定**：🟥→⏸ **接受带入 Stage-3**（已记入 must-do）。

### A3-r11〔A1 端到端连通——这是真硬门 HG-1〕🟥 缺 clk100 段证据

**蓝方本轮坦白**：r10 要的端到端 `report_timing -from construct_reg -to trace_dbg_data` **返回空**，理由是"中间有 CDC（AsyncFIFO）+ false_path 切断，不可能有贯穿 setup 路径"。

**红方判定**：这个理由**成立**——frame128 经过 AsyncFIFO 跨域，trace_clk 段与 clk100 段之间本来就没有单条 setup 路径，要求"一条贯穿路径"是 r10 红方自己提得不严谨。**红方收回 r10 对"单条端到端路径"的要求**，接受分段论证。

**但蓝方的分段只给了 2/3 段**：
- ✅ IDDR→traceIF（同 trace_clk 域）——有；
- ✅ traceIF→AsyncFIFO（跨域）——frame_cdc_tb 验了；
- 🟥 **AsyncFIFO 输出→dmux→chk→cobs→sf→trace_dbg_data（同 clk100 域）——没给证据**。

这一段**没有跨域、没有 false_path**，应该有完整 setup 路径。r09 的教训正是"模块在 ≠ 数据通路连通"——cell 计数证明不了 clk100 段的 8bit 数据线根根连到 sf_data。

**HG-1（硬门）**：补 clk100 段的端到端 timing：
```tcl
report_timing -from [get_pins -hier -filter {NAME =~ *u_frame_cdc*/m_axis_tdata_reg*/C}] \
              -to   [get_ports trace_dbg_data[*]] -max_paths 5
```
**必须返回非空 + 正 slack。** 若返回空 → clk100 段数据通路是断的（DONT_TOUCH 只保了壳），A1 未真闭环。

**判定**：🟥 **HG-1 硬门**，补 clk100 段 timing 报告即关。

---

## B 项：挂起项里哪些是买板前必做

### B1-r11〔N3 BUFR/BUFIO 敏感性〕🟥🟥 HG-2——蓝方声称交付，但文件不存在

**红方搜索结果**：`run_capture_bufr.tcl` —— **No files found**。蓝方在 13 号文档 §6 写"`run_capture_bufr.tcl`"并留"综合完成后填入对比数字"的占位符——**即这一项根本没跑，文件都没建**。

**这是本轮最该点名的事**：r10 红方已明确——trace_clk 全 BUFG 化在 100MHz 时源同步窗口紧 30-40%，**直接决定上板 deskew 能否找到 ≥8 tap 窗口**，是"这条路通不通"的关键，且**零硬件成本**。蓝方第二次把它挂起，且这次是"假装交付"（列了文件名+占位符，实际没文件没数据）。

**为什么这是买板前必做**：如果 BUFG 紧到上板根本找不到 ≥8 tap 采样窗口，那 35T 买回来是废的——而这个判断**现在用一次 OOC 综合就能做**。等上板才发现 = 浪费板钱 + 时间。这正是 r09 一路在反对的"把可提前发现的风险推到最贵阶段"。

**HG-2（硬门）**：真的建 `run_capture_bufr.tcl`，跑 BUFR/BUFIO 版 `trace_capture_a7` 的 OOC，对比 BUFG 版的：
- trace_clk→IDDR/C 的 clock skew / insertion delay；
- 在 100MHz trace_clk 约束下，IDDR 采样的 setup/hold 窗口净宽度。
给出"BUFR 比 BUFG 宽多少 ps / 多少 tap 等效"的数字。**若 BUFG 版净窗口 < 等效 8 tap（~0.62ns @78ps/tap），必须在 RTL 改 BUFR/BUFIO 后再下单。**

**判定**：🟥🟥 **HG-2 硬门**。声称交付实为占位符，必须真跑。

### B2-r11〔deskew 方案3 的隐藏依赖〕🟥→⏸ 要求补 UDP→CSR 工作量估算，不阻塞下单

**红方读了 12 号文档**。方案3（上位机辅助）选型理由站得住（架构一致、FPGA 最省、调试最快），**认可选型**。

**两个质疑**：
1. **解耦性**（鸡生蛋）：deskew 校准命令走以太网 UDP，被校准对象是 trace 采样——**两者解耦**（以太网链路独立于 trace 链路）。✅ 红方确认不死锁，这点蓝方对。
2. **隐藏工作量**：12 号文档把"复用以太网 UDP 控制通道"轻描淡写——但**当前 fpga_core 是 UDP loopback（收什么发什么），没有 UDP→CSR 写解析逻辑**。tap 写口的 300 LUT 只是 CSR 寄存器本身，**UDP 命令解析 + CSR 总线**这块 RTL 没算进去。

**判定**：🟥→⏸ **接受带入 Stage-3**，但要求 12 号文档补一句：deskew 方案3 的 FPGA 侧工作量 = tap CSR(~300 LUT) **+ UDP命令解析/CSR总线(未估)**，后者是 Stage-3 RTL，不能漏。这不阻塞下单（deskew 整体是 Stage-3），但要求账算全，避免又一次"轻描淡写"。

### B3-r11〔RGMII RX IDELAY 双轨〕🟥 HG-3——只留 parameter，=1 未综合

**红方判定**：蓝方声称留了 `PHY_RX_DELAY_INTERNAL` parameter，但 13 号文档**没有 =1 时的综合数据**。这与 P0-1 strap 耦合：若 strap 回复"RX delay 关"，蓝方必须补 5 个 IDELAYE2 + 可能的第二个 IDELAYCTRL（RGMII 数据脚在 Bank 14/15，**与 trace 的 Bank 16 IDELAYCTRL 不同 bank → 必须第二个 IDELAYCTRL**）。

**这可能触发新的布局问题**（第二个 IDELAYCTRL 能否 place 到 RGMII 数据脚 bank），**现在用一次综合就能验，不该等 strap 回复**——两种 strap 情况都要提前验，避免回复来了才发现资源/布局崩。

**HG-3（硬门）**：跑 `PHY_RX_DELAY_INTERNAL=1` 的综合，确认：
- ① 资源不破 35T（多 5 个 IDELAYE2 是 IO 硬核，LUT 影响小，主要看是否引入额外控制逻辑）；
- ② 第二个 IDELAYCTRL 能正确 place 到 RGMII 数据脚所在 bank（`report_utilization` + 确认 IDELAYCTRL location 无冲突）。

**判定**：🟥 **HG-3 硬门**。两种 strap 情况都要提前验，不等回复。

---

## C 项：本轮修复引入的新退化

### C1-r11〔fr_avail_iso 上电态〕🟡 真问题但低危，要求文档分析，不作硬门

**红方实读 RTL 确认**：`reg fr_avail_iso, fr_avail_q;` 两级都**无 reset**（reset-less，为清 REQP-1839）。

**质疑成立**：reset-less flop 上电是不定态。真实硅片上电瞬间 `fr_avail_iso` 从 X→实际值的跳变，会被 `fr_avail_iso ^ fr_avail_q` 检测成一次 toggle → **产生一个伪 frame_strobe → 往 FIFO 写一帧垃圾**。

**危害评估（红方反算）**：
- 上电时 `sys_rst` 高，`trace_lost_cnt` 被 reset 清零；但 frame_strobe 不受 sys_rst gate（iso/q 无 reset）；
- 伪帧写进 FIFO → clk100 段解出一个垃圾 TPIU 帧 → traceIF/tpiu 的 sync 机制会在下一个真 sync 字（0x7fffffff）重新对齐，**垃圾帧最多污染开头一两帧**；
- **低危**：trace 解码本来就有 sync 重建机制，上电一个伪帧会被 resync 吃掉。

**但 frame_cdc_tb 没覆盖这个**（tb 里 `fr_avail=0` 初值确定，测不出 X 上电）。**iverilog 2-state 也测不出 X**。

**判定**：🟡 **真问题、低危**。要求蓝方在文档分析上电瞬态（或给 fr_avail_iso 一个 reset——但那又触发 REQP-1839，所以 reset-less 是权衡）。**不作硬门**（危害被 sync 重建吸收），但要写进 Stage-3 上板首检"上电后确认首帧 sync 正常建立"。

### C2-r11〔MDIO IOBUF 推断〕🟡 要求确认综合推断 IOBUF，不作硬门

**红方实读 RTL 确认**：
```verilog
assign mdio_mst_oe  = 1'b0;   // 恒 0
assign phy_mdio = mdio_mst_oe ? mdio_mst_o : 1'bz;   // oe 恒 0 → 恒高阻
assign mdio_mst_i = phy_mdio;
```

**质疑成立**：`mdio_mst_oe` 恒 0 → `phy_mdio` 恒 `1'bz` → 综合器**可能**把这个 inout 优化成纯输入（因为 oe 永不为 1，输出路径是 dead code），导致 Stage-3 接 FSM 时 IOBUF 的输出端不存在、接口对不上。

**判定**：🟡 **要求确认**。蓝方需跑 `report_property [get_ports phy_mdio]` 或看综合 log，确认推断出 **IOBUF**（带 T/I/O 三端）而非 IBUF。**不作硬门**（Stage-3 接 FSM 时若发现是 IBUF，改一下约束/RTL 即可，且 mdio 不影响 Stage-2 资源/时序结论），但写进 Stage-3 must-do。**或**更干净的做法：现在就让 `mdio_mst_oe` 由一个真实（哪怕常 0 的）寄存器驱动并加 `KEEP`，强制 IOBUF 推断。

---


## D 项：收敛判断（下单前硬门 vs 带入 Stage-3 的风险）

### D1：开放项分类——纯仿真综合可关 vs 必须上板

| 开放项 | 纯仿真/综合可关？ | 归类 |
|---|---|---|
| A1 clk100 段连通（HG-1） | ✅ 一条 report_timing | **下单前硬门** |
| N3 BUFR 窗口余量（HG-2） | ✅ 一次 OOC 综合 | **下单前硬门** |
| RGMII RX IDELAY 双轨（HG-3） | ✅ 一次参数综合 | **下单前硬门** |
| D2 35T/100T 兼容（HG-4） | ✅ UG475 pinout 自查（不必等客服） | **下单前硬门** |
| P0-1 PHY strap **实际值** | ❌ 板上测 / 客服 / 原理图 | 带入 Stage-3（MDIO 兜底已在） |
| 上板眼图 ≥8 tap 窗口 | ❌ 必须上板 + 真实信号 | 带入 Stage-3 |
| 亚稳态 MTBF | ❌ 成熟核 + 上板老化 | 带入 Stage-3 |
| trace_lost_cnt 进 OrbFlow | ❌（是设计工作非验证） | Stage-3 设计 |
| deskew UDP→CSR RTL | ❌（Stage-3 实现） | Stage-3 设计 |
| fr_avail_iso 上电态 | 🟡 文档分析即可 | Stage-3 首检 |
| MDIO IOBUF 推断 | 🟡 一条 report_property | Stage-3 首检 |

**关键收敛点**：剩下"纯综合就能关"的只有 **4 个硬门（HG-1~4）**，全部可穷举、可在几次 Vivado run 内完成，**不需要买板、不需要客服**（HG-4 的 UG475 自查可独立于客服）。

### D2：下单前硬门（精确到命令，可穷举）

**HG-1 · A1 clk100 段连通**
```tcl
report_timing -from [get_pins -hier -filter {NAME =~ *u_frame_cdc*/m_axis_tdata_reg*/C}] \
              -to   [get_ports trace_dbg_data[*]] -max_paths 5
```
关闭条件：返回**非空 + 正 setup slack**。

**HG-2 · N3 BUFR/BUFIO 敏感性**（真建文件，不是占位符）
建 `run_capture_bufr.tcl`，BUFR/BUFIO 版 vs BUFG 版 `trace_capture_a7` OOC，报 trace_clk→IDDR/C 的 skew + 100MHz 下 IDDR setup/hold 净窗口。
关闭条件：给出净窗口 ps 数；**若 BUFG 版 < ~0.62ns（等效 8 tap）则必须切 BUFR/BUFIO 后再下单**。

**HG-3 · RGMII RX IDELAY 双轨综合**
```tcl
synth_design -top trace_probe_top -generic PHY_RX_DELAY_INTERNAL=1 ...
report_utilization ; # 确认不破 35T
# 确认第二个 IDELAYCTRL place 到 RGMII bank 无冲突
```
关闭条件：=1 版本资源不破 35T 且 IDELAYCTRL 布局无冲突。

**HG-4 · 35T/100T 兼容自查**
下载 Xilinx UG475 FGG484 pinout，cross-check 两个 die 在用到的脚（D17/F13/E14/D14/E16 + 14 个 dbg 脚 + RGMII 脚）的 BANK/IO 类型/MRCC 是否一致。
关闭条件：一致性对比表（不必等客服）。

### D3：下单决策——35T GO/NO-GO

**红方建议：35T，补完 HG-1~4 即 GO。不需要上 100T。**

理由：
- 完整版预估 **17.4%（方案3 deskew）~24%（保守）LUT**，35T 物理余量充足，100T 多 4 倍 LUT 对本用例无价值；
- 唯一可能逼向 100T 的是"资源破板"，而 HG-3（RGMII 双轨）是最后一个可能推高资源的变量——它一旦确认不破 35T，资源维度就锁定了；
- 时序维度：WNS=+1.254ns / WHS=+0.034ns（蓝方本轮数字），有正余量。**WHS 只有 34ps 偏紧**，但都是 false_path 之外的真实路径，且 Stage-3 加 deskew/UDP 桥后要重看——**这也是为什么 HG-2 的 BUFR 窗口必须现在验**（窗口是上板成败的物理底线）。
- 35T 与 100T 同 FGG484 封装，HG-4 确认兼容后，**即使 Stage-3 意外破 35T，也能换 100T 不改 PCB**——这是 35T 的安全垫，进一步支持"先 35T"。

---

## 最终下单决策与给红方的建议

**NO-GO → 补完 HG-1/2/3/4 四个硬门后 GO（35T）。红方在此承诺：这 4 个硬门是最后的工程必补项，补完即下单，不再新增。**

补完后接受带入 Stage-3 的已知风险清单（红方明确背书这些是"真实硬件风险，仿真综合无法提前关"）：
1. PHY strap 实际值（MDIO 兜底接口已就位，HG-3 已预验两种情况的资源）；
2. 上板眼图能否找到 ≥8 tap 窗口（HG-2 已用综合预估窗口余量，但真实信号要上板）；
3. AsyncFIFO Gray 指针亚稳态 MTBF（靠成熟核 + 上板老化）；
4. deskew UDP→CSR 通道、trace_lost_cnt 进 OrbFlow、fr_avail 上电态、MDIO FSM——均 Stage-3 设计/首检项。

**给蓝方的话**：这三轮是有价值的——NEW-1 那个无声丢帧 bug 如果带到上板，会被误诊成信号完整性问题、浪费一周，现在它在仿真里就被 counter 锁死了。`frame_cdc_tb` 的 5 次 FAIL 调试记录是这轮最有说服力的东西。**但 N3 这种"列了文件名 + 占位符却没真跑"的交付方式要停**——它是零成本就能做的关键敏感性，两轮没做，第三轮假装做了。补完 HG-1~4（都是几次 Vivado run 的事），下单。

**一句话**：第三轮收敛——P0 真修了（CDC 行为验证是实打实的进步），剩 4 个纯综合就能关的硬门（其中 HG-2 是蓝方占位符假交付的 BUFR 窗口余量，必须真跑），补完即 GO 35T，PHY strap / 上板眼图 / 亚稳态 MTBF 作为真实硬件风险带入 Stage-3。不会有第四轮工程拉锯——除非 HG-1~4 的结果本身暴露新的物理破板事实。
