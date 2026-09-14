# r10 · Stage-2 修复复审（不接受自证，只认可独立复现的证据）

> 评审对象：`proposals/11-r09回应-真实数据落地修复.md` + 实际改动的 RTL/xdc/tcl
> 已独立读取核对：`trace_probe_top.v`、`trace_probe.xdc`、`trace_capture_a7.v`、`run_top_impl.tcl`
> 立场：红方。本轮比 r09 更严——**蓝方标"已关"不算关；只有红方能独立复现的证据才算关。Refuse to rubber-stamp。**

---

## 总体结论：NO-GO（不可下单），且"已关 9 项"中我只认可 5 项真正闭环

蓝方这一轮的修复**方向全部正确**，RTL 改动是真做了（DONT_TOUCH、AsyncFIFO、IDELAYCTRL 同步链、set_input_delay、引脚重分配都在代码里看得到，不是 PPT）。这点先认。

但用红方标准复核后：

- **真正闭环（认可关闭）**：N2（IDELAYCTRL 同步释放）、D3（set_input_delay + 诚实 false_path）、A2（完整版口径修正）、B1（corner 注明）、C2（IDDR 一拍偏移注明）——**5 项**。
- **🟥 不接受关闭（蓝方标了✅但缺独立证据，或修复本身引入新问题）**：A1（pipeline 存活——SURVIVAL CHECK 自相矛盾）、B2（CDC——多 bit 跨域结构性修了，但引入**新的无反压丢帧 bug**且无双时钟回归）、D1（引脚 bank——只有 xlsx 抄录，无 Vivado property 证据）——**3 项**。
- **🟥 P0 外部依赖（蓝方诚实挂起，我同意挂起但补一条工程兜底要求）**：P0-1（PHY strap）、D2（35T/100T 兼容）——**2 项**。
- **新发现的退化/bug**：见专节——**AsyncFIFO tready 被忽略 + 单周期 tvalid（无反压丢帧）**、**MDIO 仍 1'bz（strap 兜底无法实现）**、**SURVIVAL CHECK 脚本逻辑自身有缺陷**。

**一句话**：r09 的三个 P0 里，B2 的"多 bit 裸跨域"在结构上确实被 AsyncFIFO 修对了（这是真进步），但 A1 的"pipeline 存活"蓝方自己的证据脚本就自相矛盾、B2 顺手引入了一个新的无反压丢帧 bug、D1 的 bank 仍是抄表не核。下单前要补的不止 2 个客服回复。

---


## A · 修复"有效性"独立验证

### A1-Recheck〔🟥 不接受关闭〕trace pipeline 真进 routed netlist 了吗？

**蓝方做了什么**：
- 6 个实例加 `(* DONT_TOUCH = "true" *)`（已在 `trace_probe_top.v` 确认，每个实例上方都有）；
- 加 14 个 `trace_dbg_*` 顶层输出端口，接到 GPIO1 Bank 16 引脚（xdc 已确认引脚分配）；
- `run_top_impl.tcl` 末尾加 SURVIVAL CHECK，报 6 模块 cell 计数 583 LUT。

**红方独立读代码后的判定**：cell 计数有了，但**蓝方自己的脚本同时输出了一个直接矛盾的信号，蓝方在报告里没解释**。

读 `run_top_impl.tcl` 末尾的 `TRACE PIPELINE TOP-LEVEL SIGNAL CHECK` 块：它对 `fr_avail / frame128 / fr_pulse / dmux_out_valid / chk_out_valid / cobs_out_valid / sf_out_valid` 逐个查 net 是否存活。蓝方 r09 复审里红方就引用了这个块的输出——**这些 net 报 `*** OPTIMIZED AWAY ***`**。

**这构成 cell 存活 vs net 消失的矛盾**，有两种解释，蓝方必须证明是哪一种：

1. **良性解释**：`get_nets -hierarchical -top_net_of_hierarchical_group "trace_probe_top/$sig"` 这个查询命令的语义本身就不对——DONT_TOUCH 把模块边界保住后，顶层 wire 名经过 synth 可能被改名/合并，`get_nets` 用错了路径前缀查不到，但 net 实际在。**这是脚本 bug，不是设计 bug。**
2. **恶性解释**：cell 被 DONT_TOUCH 保住了"外壳"（模块作为黑盒保留，cell 计数 >0），但**模块内部到模块之间的数据通路被推空**——比如 `frame128` 这条 128 bit 真的没连进 u_dmux，pipeline 是一串孤岛。

**DONT_TOUCH 的已知语义恰恰支持恶性解释的可能性**：DONT_TOUCH 阻止"模块整体被优化掉"，**但不保证模块之间的互连 net 被保留**——如果 opt_design 判定 u_dmux 的 `in_frame` 输入对其可观测输出无影响（因为下游 `out_ready=1'b1` 写死 + 中间数据没全引出），它可以把模块保留为壳、却把喂进去的 frame_lat 连线优化成常量。**cell 计数 583 LUT 不能排除这种情况。**

**红方要求的独立证据**（蓝方说"关"不算关）：
```tcl
# 跨整条 pipeline 的端到端 timing 路径，必须返回非空且 delay 合理：
report_timing -through [get_pins u_capture/u_iddr*/Q1] \
              -through [get_pins -hier -filter {NAME =~ *u_traceif*construct*}] \
              -through [get_pins -hier -filter {NAME =~ *u_sf*}] \
              -to [get_ports trace_dbg_data[*]] -max_paths 5
```
若这条 `report_timing -through ... -to trace_dbg_data` **返回 "No timing paths found"**，说明 pipeline 数据通路是断的，DONT_TOUCH 只保住了形——**与 r09 之前本质相同，只是这次 LUT 数字"看起来"诚实**。

**附带反算（支持需要进一步证据）**：u_traceif 实测 142 LUT vs T3 单独 OOC 119 LUT，只多 19%。但顶层集成通常因为 DONT_TOUCH **抑制跨模块共享优化**，膨胀应更明显（B1-Recheck 详述）。19% 偏小，**与"内部数据路径可能被简化"的疑点一致**。

**判定**：🟥 **不接受关闭**。必须补 `report_timing -through ... -to trace_dbg_data[*]` 的端到端路径报告（非空 + delay 合理），证明是良性的脚本 bug 而非数据通路断裂。

### A2-Recheck〔🟥 待补〕AsyncFIFO 自身的 BRAM 占用到底几块？

**蓝方称**：BRAM 9→11（+2）= axis_async_fifo 占用。

**红方反算**：`axis_async_fifo DEPTH=16 × DATA_WIDTH=128 = 2048 bit`。一个 RAMB36 = 36Kbit，**一个 RAMB18 = 18Kbit 就装得下 2048 bit**。所以 AsyncFIFO 本体**最多 1 块（甚至半块 RAMB18）**，不该是 +2。

**矛盾点**：蓝方 SURVIVAL CHECK 里 `u_cobs` 报 **2 BRAM**。而 r09 时的 8.5 BRAM 是 trace pipeline 被剪后的"以太网 only"数。所以这一轮 +2.5 BRAM 实际是：**cobs 的 2 块（pipeline 存活后才出现）+ AsyncFIFO 的 ~0.5~1 块**——**不是蓝方说的"+2 正好是 AsyncFIFO"**。蓝方把 cobs 复活的 BRAM 错记成了 AsyncFIFO 的占用。

**这不是致命问题（总数 11 块仍在 35T 的 50 块内）**，但说明蓝方的 BRAM 归因是错的，且再次侧面印证 A1——cobs 这 2 块 BRAM 的出现恰恰说明 cobs **可能**真活了（有 RAM 存储）。但"可能"不够。

**红方要求**（也回答 B1-Recheck 的 cobs FIFO 验证）：
```tcl
get_cells -hier -filter {REF_NAME =~ RAMB* && NAME =~ *u_frame_cdc*}   ;# AsyncFIFO 的 RAMB，数出来
get_cells -hier -filter {REF_NAME =~ RAMB* && NAME =~ *u_cobs*}        ;# cobs 的 RAMB，列 NAME
```
分别数清楚，把 BRAM 归因写对。

**判定**：🟥 **待补**。归因错误，要逐 cell 列出。

### A3-Recheck〔🟥 待补〕`set_false_path -hold` 覆盖范围是否过宽？

**蓝方做了什么**：`set_false_path -from [get_ports {trace_data_in[*]}] -hold`。

**红方判定**：按 SDC 语义，`-from [get_ports trace_data_in[*]]` 只覆盖**以这些端口为起点**的路径。IDDR 之后到 traceIF 的路径起点是 `IDDR/Q1`（一个时序单元的输出），**不是端口**，所以理论上不受这条 false_path 影响。**蓝方的写法方向是对的。**

**但 Vivado 的实际行为需要验证**，原因有二：
1. `trace_data_in → IDELAYE2 → IDDR/D` 这条路径，IDDR 是组合性 capture，Vivado 在 source-sync 场景下有时会把 input delay 的影响传播到 IDDR 输出后的第一级——需确认 false_path 没意外延伸；
2. 更关键：**这条 false_path 与 `set_clock_groups -asynchronous` 里把 `trace_clk_in` 整组设为异步是叠加的**。`trace_clk_in` 域内部（IDDR/Q1 → traceIF.construct）的 hold 检查，**到底还在不在？** 如果 set_clock_groups 已经把整个 trace_clk 域与所有其它域设为异步，那 trace_clk 域**内部**的 hold 仍应被检查（同域内部不受 clock_groups 影响）——但要确认。

**红方要求**：
```tcl
report_exceptions -summary          ;# 看 false_path 影响的 endpoint 数
report_timing -hold -from [get_pins -hier -filter {NAME =~ *u_iddr*/Q1}] \
              -to [get_pins -hier -filter {NAME =~ *u_traceif*}] -max_paths 5
```
第二条必须返回**非空且 hold 正余量**，证明 IDDR→traceIF 的同域 hold 仍被检查且通过。

**判定**：🟥 **待补 `report_exceptions` + 同域 hold 路径报告**。写法看着对，但 r09 的教训就是"看着对的约束被 set_clock_groups 悄悄架空"——这次必须验证，不能再凭语义推断。

---


## B · 修复"完整性"独立验证

### B1-Recheck〔🟥 待补〕14 个 dbg 端口是否真锁住了内部数据路径？

**蓝方做了什么**：引出 `trace_dbg_data[7:0]`（=sf_data）、`trace_dbg_valid/last`、`trace_dbg_inter[3:0]`（={dmux,chk,cobs,fr_pulse} 的 valid）。

**红方判定**：valid/控制信号被引出了，**但中间 8 bit 数据线（dmux_data / chk_data / cobs_data）没有一根被引到顶层**。只有最末级 `sf_data[7:0]` 出去了。

**这给 opt_design 留了一个口子**：DONT_TOUCH 阻止模块整体被删，但**不阻止模块内部的数据位做常量传播/逻辑化简**。具体地：
- u_dmux/u_chk/u_cobs 的 8 bit 输出数据，只有最终汇到 sf_data 才被观测；
- 但因为每一级的 `out_ready` 都写死 `1'b1`（见 RTL，u_dmux/u_chk/u_cobs/u_sf 的 out_ready 全是 1'b1），**反压链是断的**；
- 在没有反压、且中间数据不被独立观测的情况下，综合器**可能**把某些级的数据通路简化（只要最终 sf_data 的值在它分析的激励下可被等价计算）。

**关键反算**：u_traceif **142 LUT** vs T3 单独 OOC **119 LUT**，只多 **19%**。蓝方解释为"展开 + 顶层 wire + DONT_TOUCH 抑制共享优化"。但 DONT_TOUCH **抑制共享优化的方向是让 LUT 变多**（不能跨模块合并），加上顶层布线，正常应该膨胀 **30~50%**。**只多 19% 是偏低的**，与"内部部分数据路径被简化"的疑点方向一致——不能排除。

**红方要求**（蓝方说"关"不算关）：
1. 把 routed checkpoint 存出来 `write_checkpoint -force routed.dcp`，GUI 打开手工目视追 `u_cobs/.../out_data` → `u_sf/.../in_data` → `sf_data` → `trace_dbg_data`，确认 8 bit 数据线根根连通；
2. 或验 cobs 的 BRAM 真有存储功能：`report_property [get_cells -hier -filter {REF_NAME=~RAMB* && NAME=~*u_cobs*}]`，确认 BRAM 的读写口都接了真实地址/数据，不是只剩控制逻辑的空壳。

**判定**：🟥 **待补数据通路连通性证据**。这是 A1 的同一个根问题的另一面——cell 在 ≠ 数据路径完整。

### B2-Recheck〔🟥🟥 结构修对了，但引入新 P0：无反压丢帧〕

**蓝方做了什么**（结构层面，认可）：用 `axis_async_fifo DEPTH=16 DATA_WIDTH=128` 替换原来的 2-FF + frame_lat 手写 CDC。**128 bit 用 Gray 码指针 + BRAM 跨域，这在结构上确实修掉了 r09 B2 的"多 bit 裸跨域"——这是真进步，认可。**

**但红方读 RTL 后发现修复过程引入了三个新问题，其中一个是 P0：**

#### 🟥🟥 新 P0：`s_axis_tready` 被忽略 + 单周期 `tvalid` = 无声丢帧

读 `trace_probe_top.v`：
```verilog
reg fr_avail_q;
always @(posedge trace_clk) fr_avail_q <= fr_avail;
wire frame_strobe = fr_avail ^ fr_avail_q;   // 单 trace_clk 周期脉冲
...
.s_axis_tvalid (frame_strobe),
.s_axis_tready (cdc_in_ready),   // ← cdc_in_ready 在 RTL 里没有任何地方被读取
```

**问题 1（AXIS 协议违例）**：`frame_strobe` 是单周期脉冲。AXIS 协议要求 `tvalid` 一旦拉高，**必须保持到 `tready` 握手**才能撤。这里 frame_strobe 只有 1 个 trace_clk 周期——**如果那一拍 FIFO 满（tready=0），这一帧直接丢，且 tvalid 已经撤了，永远补不回来**。

**问题 2（无反压）**：`cdc_in_ready`（=FIFO 的 s_axis_tready）**在整个 RTL 里没有被任何逻辑读取**——和 r09 之前 `out_ready=1'b1` 的"耍流氓"是同一类问题，只是换了个位置。FIFO 满了上游不知道，继续丢。

**这是不是真问题？反算**：traceIF 每 ~几十个 trace_clk 出一帧（一个 16 字节 TPIU 帧）。AsyncFIFO DEPTH=16。正常情况下 clk100 侧消费够快（dmux out_ready=1），不会满。**但**：
- trace_clk 满速 100MHz，clk100 也是 100MHz，但 dmux 处理一帧要多个周期（unmangle→serializer→...逐字节出 15 字节）；
- 如果 trace 突发（连续帧），FIFO 16 深度可能填满，**此时 frame_strobe 来一个丢一个，无人知晓**。

**对"抓 UAF/profiling"的影响**：和 r02/r04 反复说的一样——**无声丢帧在解码端表现为帧错位/重同步**，而你不知道是硬件采样错了还是 FIFO 溢出了。debug 噩梦。

**蓝方在 RTL 注释里写**：
```
// we let the FIFO pace itself via s_tready/back-pressure when frame128 is replicated.
```
**这句是空话**——RTL 里 `cdc_in_ready` 根本没接到 fr_avail / frame_strobe 的产生逻辑上，traceIF 也没有 ready 输入（它是 free-running 的 `always @(posedge traceClkin)`，根本不能被反压）。**所以"FIFO 自己 pace"在物理上做不到——traceIF 不接受反压。**

**正确做法**：要么
- AsyncFIFO 用 `FRAME_FIFO=1` + 监控 `s_status_overflow`，至少**把溢出事件计数并上报**（让解码端知道"这里丢了 N 帧"），这正是 orbtrace upstream `util.Monitor` 的 `lost` 信号干的事；
- 或加一个 trace_clk 域的 holding register，frame_strobe 来时若 FIFO 满则置 overflow flag。

**判定**：🟥🟥 **新 P0**。多 bit 跨域的结构修对了，但 CDC 的**握手/反压/溢出可见性**没做，等于把 r09 的"裸跨域丢帧"换成了"FIFO 满丢帧且无人知"。**这条不修，上板 debug 会把 FIFO 溢出误诊为信号完整性问题。**

#### 🟥 新问题：`sys_rst` 跨域喂给 AsyncFIFO 两侧

```verilog
.s_clk (trace_clk), .s_rst (sys_rst),   // sys_rst 是 clk100 域
.m_clk (clk100),    .m_rst (sys_rst),
```
`sys_rst` 在 clk100 域生成（`rst_sync` 在 `posedge clk100`）。它**直接喂给 trace_clk 域的 `s_rst`** 是跨域 reset。

**好消息**：红方查 verilog-ethernet 的 `axis_async_fifo.v` ——它内部对 `s_rst`/`m_rst` 各有 `sync_reset` 模块做同步（Alex Forencich 的核是工业级，这点是处理了的）。**所以这条大概率是良性的**，但蓝方**没有举证**自己知道这一点，是"碰巧用对"。

**红方要求**：蓝方贴出 `axis_async_fifo.v` 里 `s_rst → sync_reset → s_rst_sync` 的代码段，证明跨域 reset 被内部同步了（**确认是良性**）。

**判定**：🟥 **待补证据**（大概率良性，但要蓝方证明它知道为什么良性，而不是碰巧）。

#### 双时钟域回归仿真缺失（与 D1 推迟耦合）
见 D1-Recheck / C1 裁定：**B2 的结构修复没有双时钟域仿真背书**。结构对 ≠ 行为对（上面的丢帧 bug 正是结构对但行为错的例子）。

### B2-double〔回应蓝方"xsim 不算"〕
蓝方报告里 B2 验证写的是"post-impl P&R 收敛 + DRC 0 errors"。**红方明确：P&R 收敛和 DRC 干净，证明不了 CDC 行为正确**——DRC 不检查"FIFO 满时丢帧"。**必须有不等比 + 抖动的双时钟 cocotb/iverilog 回归**，注入连续 100+ 帧并制造 FIFO 接近满的场景，验证：①无丢帧或②丢帧被 overflow flag 捕获。xsim 的零抖动等比仿真不算（这正是 r09 B2 的根本教训）。

### B3-Recheck〔🟡 基本认可，补一个 MMCM 依赖盲点〕IDELAYCTRL 同步释放链

**蓝方做了什么**（`trace_capture_a7.v` 已确认）：
```verilog
reg [3:0] idc_rst_sync = 4'hf;
always @(posedge ref_200m or posedge rst)
    if (rst) idc_rst_sync <= 4'hf;
    else     idc_rst_sync <= {idc_rst_sync[2:0], 1'b0};
wire idc_rst = idc_rst_sync[3];
```
**红方判定**：async assert + sync release 到 ref_200m，**符合 UG471，结构正确，认可。**

**但红方追问的 MMCM 依赖盲点成立**：`ref_200m` 来自 MMCM 的 CLKOUT2。MMCM 未锁定时 `ref_200m` 无沿 → `idc_rst_sync` 不移位 → 永远停在 `4'hf` → IDELAYCTRL 永远 reset → `idelayctrl_rdy` 永远 0。

查 `u_capture.rst` 接的是什么：`trace_probe_top.v` 里 `.rst(sys_rst)`。而 `sys_rst = rst_sync[3]`，`rst_sync` 在 `posedge clk100`，初值 4'hf，喂 `~mmcm_locked`。**所以 MMCM 锁定前 sys_rst 保持高**——IDELAYCTRL 的 idc_rst（async assert）会被 sys_rst 持续拉高，MMCM 锁定后 sys_rst 才释放，ref_200m 也才稳定有沿。**这条链路实际上是对的**：MMCM 没锁→sys_rst 高→IDELAYCTRL 复位；MMCM 锁→sys_rst 释放 + ref_200m 有沿→同步链开始移位→idc_rst 释放。

**所以 B3 红方原来担心的死锁不成立**——sys_rst 已经 gate 了 MMCM lock。**认可关闭**，但要蓝方在文档里写明这条依赖关系（sys_rst 依赖 mmcm_locked，IDELAYCTRL 依赖 sys_rst），避免未来有人改 reset 结构时踩坑。

**判定**：🟡 **基本认可**，补一句依赖说明即可。这是本轮唯一一个"红方原质疑经独立核查后不成立"的——如实记录。

---


## C · 蓝方"诚实声明"审视

### C1〔🟥 不接受关闭〕"trace pipeline 完整存活"的最低标准

蓝方报告把 A1/B2 标 ✅ 关。**红方按最低标准逐条核**：

| 完整存活的最低标准 | 蓝方证据 | 红方判定 |
|---|---|---|
| Cell 数量合理 | 583 LUT，膨胀 18% | ✅ 达成（但 18% 偏低，见 B1） |
| End-to-end 数据通路连通 | 无 | ❌ 未验证（SURVIVAL CHECK 只看 cell，且 net check 报 OPTIMIZED AWAY） |
| set_clock_groups 后仍有真实跨域 timing 路径 | Inter-Clock 表预计为空 | ❌ 全屏蔽，Vivado 检不到 trace_clk→clk100 真实信号 |
| 双时钟域回归仿真 | 无（xsim 是单时钟/零抖动） | ❌ 未补 |

**4 条里只达成 1 条。** 蓝方凭"cell 数 + P&R 收敛 + DRC 0"就标关，**红方拒绝**。

**裁定**：A1 与 B2 **不可标记关闭**，直到蓝方补：
1. 端到端 `report_timing -through ... -to trace_dbg_data[*]` 非空路径（A 项）；
2. AsyncFIFO 双时钟域 cocotb/iverilog 回归，含 FIFO 满场景（B 项）。

### C2〔🟥 必做的设计选型，蓝方未做〕"完整版 24%" 的可信度 + deskew 方案未定

**蓝方接受 24% 反算口径**——认可这个诚实修正。

**但红方的新质疑成立且未被回应**：24% 是估算，其中最大不确定项是 **deskew FSM 的实现选择**。蓝方把它推到 Stage-3 上板再说——**这是设计选型，不该等上板**。

deskew 三种方案，LUT 与调试难度差异巨大：
| 方案 | LUT 估 | 上板调试难度 | 风险 |
|---|---|---|---|
| 全硬件 FSM（XAPP1064/585 式，每 lane 扫 32 tap + 眼心判决） | ~1,500 | 中（纯硬件，确定性） | LUT 占用最大 |
| 半硬件 + MicroBlaze/软核辅助扫描 | ~800 + 软核 | 高（软硬协同调试） | 软核又是一层 |
| 上位机辅助（PC 通过 MDIO/CSR 控 tap，PC 跑眼图算法） | ~300 | 低（算法在 PC，灵活） | 依赖上位机协议 |

**这个选型直接决定**：①完整版到底是 24% 还是更高/更低；②Stage-3 上板调试的工作量与风险。**等上板再挑 = 把一个架构决策推到最贵的阶段做。**

**红方要求**：蓝方写一份半页的 **deskew 方案选型 mini-proposal**，列上面 3 种 + 各自 LUT/调试难度/风险，**Stage-3 启动前定下来**。红方倾向"上位机辅助"（PC 算眼图最灵活，FPGA 侧最省，且符合"解码都在 PC"的一贯架构），但要蓝方论证。

**判定**：🟥 **必做选型，未做**。不阻断下单，但阻断 Stage-3 启动。

### C3〔🟥 工程兜底缺失〕RGMII RX IDELAY 依赖 PHY strap = 把工程问题甩给客服

**蓝方说**：RGMII RX IDELAY 暂不补，依赖 PHY 端 RX delay，等 strap 信息。

**红方判定**：这把一个**有确定工程解的问题**，变成了**等外部回复的阻塞项**。正确做法是双向准备（见 E1）。这条与 P0-1 耦合，详见新发现 bug 节的 MDIO 问题。

**判定**：🟥 **待补双轨 RTL**（见 E1-Recheck）。

---

## D · r09 推迟项有无被偷偷收回

### D1-Recheck〔🟥 不接受关闭〕trace 4 数据脚 bank 核对——只有 xlsx 抄录，无 Vivado 证据

**蓝方做了什么**：从 `A7_LITE_GPIO.xlsx` 抄出 GPIO1 引脚表，重新分配 trace_data 到 F13/E14/D14/E16（全 P 端），并修正了 r09 指出的"F14/E13 是 N 端 + E14 重复"的错误。**引脚重分配本身是对的改进，认可方向。**

**但红方判定**：蓝方的 bank 归属证据是**厂商 xlsx 的抄录**，不是 **Vivado device library 的 property 查询**。r09 D1 红方明确要求的是：
```tcl
get_property BANK [get_package_pins F13]    ;# 必须返回 16
get_property BANK [get_package_pins E14]
get_property BANK [get_package_pins D14]
get_property BANK [get_package_pins E16]
```
**xlsx 抄录可能有错**（厂商文档与实际 die 不一定一致，且 r09 蓝方自己就抄错过 P/N），**只有 Vivado property 查询是权威**。而且更关键的——这 4 个脚是否和 TRACECLK（D17）、和 IDELAYCTRL **同 bank**？IDELAYE2 必须与驱动它的 IDELAYCTRL 同 bank（r09 提的核心约束），xlsx 抄录证明不了这个。

**红方要求**：
```tcl
foreach p {D17 F13 E14 D14 E16} { puts "$p bank=[get_property BANK [get_package_pins $p]]" }
```
五个脚必须**全部 bank=16**，且 IDELAYCTRL 实例被 place 到 Bank 16。

**判定**：🟥 **不接受关闭**。引脚重分配是改进，但 bank 证据等级不够，必须 Vivado property 查询。

### D2-Recheck〔🟥 P0，补工程兜底〕35T/100T 引脚兼容

蓝方等客服回复。**红方同意这条需要客服，但补一条工程兜底**（见 E2）：不必纯等回复，可自查 Xilinx UG475 FGG484 两个 die 的 pinout 表 cross-check。

### D3(C1)-Recheck〔🟥 P0-2 不可关闭直到双时钟仿真存在〕

**蓝方明示 C1（xsim 1000 帧 + tap 抖动）推迟 Stage-3。** 红方 r09 允许 C1 的"扩展覆盖率"推迟。**但本轮情况变了**：B2 用 AsyncFIFO 重写了 CDC，而**这个新 CDC 从未在双时钟域被验证过**（xsim 是单 trace_clk 域 + 零抖动）。

**红方最终裁定（与蓝方明确对立）**：**P0-2 不可标记关闭，直到 AsyncFIFO 的双时钟域回归仿真存在。** 蓝方不能两边占——用 AsyncFIFO 替换了 CDC（结构修复），又把验证它的双时钟仿真推迟到 Stage-3。结构修复必须配行为验证。**要么承认 P0-2 仍开，要么现在补双时钟仿真。**

### D2(N3)-Recheck〔🟨 红方反悔，要敏感性分析〕BUFG→BUFR/BUFIO

r09 红方把 N3 列 P1 允许推迟。**本轮红方部分反悔**：trace_capture_a7 用全 BUFG 化 trace_clk，在 100MHz 时：
- 源同步窗口 = UI/2（DDR）= 2.5ns（不是 5ns，DDR 双沿，每个 bit 占半个 UI）；
- 减 BUFG insertion delay 不确定性 + jitter ~100-200ps + 数据/时钟 skew；
- BUFG 走全局网络，trace_clk 与 IDDR 的 C 脚距离远，skew 比 BUFR/BUFIO（区域时钟，就近）大。

**这直接决定 Stage-3 deskew 能否找到 ≥8 tap 的有效窗口**。BUFG 比 BUFR/BUFIO 紧 30-40%，可能让上板眼图扫描根本找不到足够窗口——而那时已经买了板、花了时间。

**红方要求**：🟨 蓝方做一次 **BUFR/BUFIO 版本的 trace_capture_a7 OOC 综合**（不入主线），贴时序对比数字，量化 BUFG 到底紧多少。**零硬件成本，现在就能做。** 这是"35T 上板能不能成"的关键敏感性。

**判定**：🟨 **待补敏感性分析**（不阻断下单，但强烈建议下单前做，因为它影响"这条路到底通不通"）。

---


## 新发现的 bug / 退化（本轮修复过程引入或暴露）

### NEW-1〔🟥🟥 P0〕AsyncFIFO 无反压 + 单周期 tvalid → 无声丢帧
见 B2-Recheck 详述。`s_axis_tready`（cdc_in_ready）在 RTL 里**从未被读取**，`frame_strobe` 是单周期脉冲不符合 AXIS valid 保持语义，FIFO 满时丢帧且无 overflow 上报。traceIF 是 free-running 不可反压，所以"FIFO 自己 pace"是空话。**必须加 overflow 计数上报（对标 upstream util.Monitor.lost）**。

### NEW-2〔🟥 P0，耦合 P0-1〕MDIO 仍 `1'bz`，strap 兜底在物理上无法实现
```verilog
assign phy_mdio = 1'bz;
assign phy_mdc  = 1'b0;
```
r09 红方就指出：若 PHY strap 不利，唯一的软件兜底是 boot 时用 MDIO 写 RTL8211E 的 RGMII delay 寄存器（Page 0xa43 reg 0x0d）。**但当前 MDIO 完全未驱动**——意味着**即使客服回复"strap 是双 delay、但可用 MDIO 改"，当前 RTL 也做不到**，因为没有 MDIO 主控。

**这把 P0-1 从"等回复"变成了"等回复 + 还要写 MDIO 主控"**。蓝方应该**现在就预留 MDIO 主控接口**（哪怕先 stub），否则 strap 不利时会发现连补救手段都没有。

**红方要求**：在 RTL 预留 MDIO master（可先不实现具体寄存器序列，但接口和三态控制要在），确保上板能动态配 PHY。

### NEW-3〔🟥 证据脚本缺陷〕SURVIVAL CHECK 的 net 查询与 cell 查询自相矛盾且未交叉验证
`run_top_impl.tcl` 的两个检查块：cell check 报"存活"，net check 报"OPTIMIZED AWAY"。**蓝方在报告里只引用了对自己有利的 cell check，没有解释 net check 的矛盾输出。** 这本身是证据呈现的不诚实（选择性引用）。脚本的 `get_nets -top_net_of_hierarchical_group` 用法对 DONT_TOUCH 黑盒大概率查不对——**要么修脚本让两个 check 一致，要么解释为什么 net 名变了**。在解释清楚前，SURVIVAL CHECK 不能作为 A1 闭环证据。

### NEW-4〔🟡 提醒〕`set_clock_groups -asynchronous` 把 7 个时钟全部互设异步——RGMII 内部同步路径也被放过
xdc 把 `sys_clk_50 / trace_clk_in / phy_rx_clk / CLKOUT0-3` 全部互设 asynchronous。这对 trace_clk↔clk100 是有意的（有 AsyncFIFO）。**但 RGMII 的 phy_rx_clk → clk125(CLKOUT0) 之间本应有真实的源同步关系**（RGMII RX 是源同步接口），全设 async 等于**放弃 RGMII RX 的 setup/hold 检查**。verilog-ethernet 的 RGMII 内部用 IDDR + 自己的约束处理，可能不依赖这条，但**蓝方把它一刀切设 async 是否影响 RGMII 内部时序约束的有效性，需确认**。这与 B3(r09) 的"set_clock_groups 架空检查"是同一类风险，换到了以太网侧。

---

## Stage-2 终审 GO/NO-GO

**NO-GO（不可下单）。**

理由不再是 r09 的"数字虚假"，而是：
1. **A1/B2 的"已关"缺独立证据**——pipeline 端到端连通性未证、CDC 双时钟行为未验，蓝方自己的 SURVIVAL CHECK 脚本还自相矛盾；
2. **修复引入新 P0**（NEW-1 无反压丢帧）；
3. **strap 兜底物理上做不到**（NEW-2 MDIO 未驱动）；
4. 两个外部依赖（P0-1/D2）仍在等回复。

**但要明确认可蓝方的真实进步**：多 bit 裸跨域（r09 最硬的 P0）在结构上确实用 AsyncFIFO 修对了；DONT_TOUCH + dbg 端口 + set_input_delay 的组合让 LUT 数字从"假"变"大概率真"；引脚 P/N 错误修正了；IDELAYCTRL 同步链做对了。**方向全对，执行差临门一脚的验证证据。**

---

## 下单前的最低硬证据集（蓝方说"关"不算，要红方能独立复现）

### 必须补的工程证据（不补不下单）
1. **A1 端到端路径**：`report_timing -through [u_capture IDDR/Q1] -through [traceIF construct] -through [u_sf] -to [trace_dbg_data[*]]` 返回**非空 + delay 合理**。贴完整输出。
2. **NEW-1 反压/溢出**：AsyncFIFO 加 `s_status_overflow` 计数并引到 dbg 端口；或改 FRAME_FIFO 模式。RTL + 重综合资源。
3. **B2 双时钟域回归**：cocotb/iverilog，trace_clk 与 clk100 **不等比 + 加抖动**，连续注入 ≥100 帧，**含 FIFO 接近满的场景**，验证无丢帧或丢帧被 overflow 捕获。**xsim 零抖动不算。**（此项闭环前 P0-2 维持 OPEN）
4. **D1 bank 证据**：`get_property BANK [get_package_pins {D17 F13 E14 D14 E16}]` 全部 =16 的输出。
5. **A3 false_path 范围**：`report_exceptions -summary` + IDDR→traceIF 同域 hold 路径非空且正余量。
6. **NEW-2 MDIO 主控**：RTL 预留 MDIO master 接口（可 stub）。
7. **A2/B1 数据通路连通**：routed.dcp 目视追 8 bit 数据线连通，或 cobs BRAM 的 read/write 口 property 证据。

### 必须补的设计选型（不阻断下单，阻断 Stage-3 启动）
8. **C2 deskew 方案 mini-proposal**：3 方案对比 + 选定。
9. **C3/E1 RGMII RX IDELAY 双轨 RTL**：`parameter PHY_RX_DELAY_INTERNAL`，跑 =1 版本综合确认不破 35T。

### 强烈建议（影响"这条路通不通"的判断）
10. **N3 敏感性分析**：BUFR/BUFIO 版 trace_capture_a7 OOC 时序 vs BUFG 版对比。
11. **NEW-4**：确认 RGMII RX 源同步约束没被 set_clock_groups 架空。

### 外部依赖（客户层 + 工程兜底）
12. **P0-1 PHY strap**：客服回复 RTL8211E strap 默认值；**且** NEW-2 的 MDIO 主控就绪作为兜底。
13. **D2 引脚兼容**：客服回复 **或** 自查 UG475 FGG484 两 die pinout cross-check 表。

---

## 关于 35T vs 100T（不变）

完整版预估 24% LUT / 26% BRAM（蓝方已接受口径），35T 仍是合理选择。**但前提是上面 1-7 的工程证据补完后，确认 pipeline 真连通、CDC 真不丢帧**——如果 A1 端到端路径证明数据通路其实是断的，那当前所有占用率数字又要重算。**先证明设计是功能完整的，再谈它占 35T 多少。**

---

## 一句话总评

**蓝方把 r09 最硬的多 bit 裸跨域用 AsyncFIFO 在结构上真修了，DONT_TOUCH/引脚/IDELAYCTRL 也都做了——方向全对，这是实打实的进步。但红方按"独立复现才算关"的标准，A1（pipeline 连通性蓝方自己的脚本就自相矛盾）、B2（结构修了却引入无反压丢帧新 P0、且无双时钟回归）、D1（bank 只抄表没查 Vivado）三项不接受关闭，外加 MDIO 未驱动让 strap 兜底落空。下单要补的不是 2 个客服回复，是 7 条工程证据 + 2 个客服回复。Refuse to rubber-stamp——把端到端路径报告和双时钟域回归这两条硬证据拿出来，再谈下单。**
