# r09 · Stage-2 终审：真实数据落地评审

> 评审对象：`PLAN_STAGE2.md`（蓝方 Stage-2 交付报告）+ `syn/artix7/rtl/trace_probe_top.v` + `syn/artix7/rtl/trace_capture_a7.v` + `syn/artix7/constraints/trace_probe.xdc`
> 立场：红方。本轮专审"真实数据"是否真的能撑住"锁 35T 下单"这个决定。
> 红方原则：**OOC/post-impl 数字漂亮 ≠ 上板能跑**；逐条独立质疑，不替蓝方挡。

---

## 总体结论

**🟥🟥 不通过 / 待补关键证据后才可下单。**

蓝方拿到了真实数字，把 r08 估算的悲观区间打回——这是六轮里第一次有真实数据落地，**要先把这个进步记上**。但读完 RTL 与 xdc 后，发现的不是"小瑕疵+Stage-3 优化项"，而是**两个当前 broken 的 P0 bug**和**一个会让 35% 综合数字失真**的根因：

- 🟥🟥 **P0-1**：RGMII TX 路径**双重 delay**。`fpga_core` 的 RGMII MAC 内部已带 RX IDELAY、TX 90° 移相（NexysVideo 配置），但 RTL8211E 在 A7-Lite 上的 strap 默认是 **TX/RX delay 都开**。Vivado 在 OOC/post-impl 时序里看不到这条——**上板第一刀就会撞**。
- 🟥🟥 **P0-2**：`fr_avail` 单 bit toggle 同步器 + `frame_lat[127:0]` 多 bit 不握手数据通路，**这是教科书级的 CDC 错误**，xsim 跑通只是因为 testbench 太理想。蓝方在文档里把它列为"留 Stage-3 改 AsyncFIFO"——但这不是优化，是**当前 RTL 的功能 bug，会随机吞帧**。
- 🟥 **数据失真根因**：`fpga_core.sw = {3'b0, sf_out_valid, sf_data[3:0]}`——把整条 trace 流水线的可观测面缩成 5 bit，且终点是 `fpga_core` 内部的 LED/sw 处理逻辑（不是 UDP TX），存在被综合工具按"常驻信号但终端无副作用"剪枝的风险。**当前 2,161 LUT 不能保证整条 trace 流水线还在 routed netlist 里。**

只要这三件不出对应证据/修复，"35T 够用"这个结论就**没有真实数据支撑**——它支撑的是"以太网 + 一个可能被剪掉的 trace 流水线"的占用。

下面逐条。


---

## A · 资源数据可信度

### A1〔🟥 待补证据〕2,161 LUT 是否真包含完整 trace 流水线？

**问题**：`trace_probe_top.v` L246 把整条 trace 流水线的输出这样喂进 `fpga_core`：

```verilog
.sw          ({3'b0, sf_out_valid, sf_data[3:0]}),
```

而 `fpga_core` 的 `sw` 在 NexysVideo example 里只接到 LED 显示和一些杂项，**不是 UDP 发送数据通路**。这就把 8 bit 数据 + 1 bit valid 的有效流"压缩成 5 bit"喂进一个**接近常量观测点的输入**。Vivado 的 `opt_design`（默认 `Default` directive）在以下条件下会做 retarget/sweep/propagate：

1. 流水线终点没有有效输出引脚副作用 → 被认为 dead；
2. 输入是非常少 bit 数 → 流水线内部很多 bit 被发现不可观测；
3. 反压口（`out_ready`）写死 1'b1 → 不阻塞，整条流水线可被推成纯组合或被剪掉中间寄存器。

**Trace core 实际逻辑量是 494 LUT（T3 OOC 实测，已包含 traceIF + tpiu_demux + checksum + cobs + super_framer）**。但在 T4 顶层综合里，受`opt_design` 优化后，**有多少剩在 routed netlist 里、有多少被剪掉**，文档没给证据。

**反算**：T1 单独 OOC = 1,871 LUT。T3 单独 OOC = 494 LUT。T2 = 0 LUT（IO 硬核）。三者相加 = **2,365 LUT**。T4 实测 = **2,161 LUT < 2,365**。差出来的 ~200 LUT 不算多，但这个差**只能由两种解释**：① 顶层做了一些跨模块共享/常量传播；② trace 流水线被部分剪掉。**当前数据无法区分这两种。**

**要求蓝方补**：
- `report_design_analysis -hierarchical -name routed_design`，证明 `u_dmux / u_chk / u_cobs / u_sf / u_traceif` 五个实例在 routed netlist 中**仍然存在且 cell count 与 OOC 一致**（容许 ±10%）。
- 若 cell count 显著低于 OOC，必须改顶层让 SF 输出真正进入 UDP 数据通路（哪怕是接到 fpga_core 一个 axis_out_data 端口），不能用 `sw[]` 假喂。
- 跑一次 `opt_design -directive ExploreArea` vs `Default`，对比 trace 模块 cell 数量差，作为是否被优化的二次证据。

**判定**：🟥 **待补**。在拿到 `report_design_analysis` 之前，"2,161 LUT 包含完整 trace 流水线"是无法独立验证的宣称。

---

### A2〔🟥 待补依据〕89% 余量真能装下 deskew + AsyncFIFO + UDP 桥 + 实际深的 buffer？

**蓝方估算**：deskew ~+500 LUT，CDC AsyncFIFO ~+200 LUT，UDP-trace 桥 ~+500 LUT。

**红方反算**（用 r08 同款"取上沿 + 锚定开源参考"）：
- **deskew FSM**：5 lane × 32 抽头扫描 + 训练图案对齐 + 眼心搜索 + 阈值判决 + 错误计数。这不是一个状态机，是**5 个并行训练 lane + 1 个全局仲裁 + 1 个 IDELAY tap 写口**。参考 Xilinx XAPP1064/XAPP585 的训练逻辑量级，**完整 deskew 1,000~2,000 LUT** 是合理上沿。蓝方 +500 是骨架，不是完整版。
- **AsyncFIFO**：`stream.AsyncFIFO` 在 amaranth 里包含 Gray 码 ptr + 双 sync chain + BRAM 端口管理，**单一 128 bit 宽 FIFO 量级 200~400 LUT + 1 BRAM**。蓝方 +200 LUT 没算 BRAM。
- **UDP-trace 桥**：要做 byte stream → UDP packet 的分包/打包/长度/反压/超时 flush。参考 verilog-ethernet 自带的 `udp_complete` 已在 1,871 里，但**应用层 byte→UDP 这层适配还要写**——不是 +500，是 **400~800 LUT + 至少 1 BRAM (TX FIFO)**。

**累加上沿**（按红方反算）：
- 当前 T4 实测：2,161 LUT / 8.5 BRAM
- + deskew 完整版：+1,500 LUT
- + AsyncFIFO：+300 LUT, +1 BRAM
- + UDP 桥 + TX FIFO：+600 LUT, +2 BRAM
- + 时序膨胀 10–15%（蓝方目前 WNS=+1.151ns，加完上面三块后路径变长，phys_opt 会复制）：+500 LUT

**预估 Stage-3 完整版**：~**5,061 LUT（24% of 35T）+ 12.5 BRAM（25%）**。

**仍然在 35T 之内**——这个结论方向对，红方认可。但"89% 余量"这种说法是**用骨架占用率讲完整版能力**，不是诚实口径。

**要求蓝方补**：
- 把 deskew/AsyncFIFO/UDP 桥三项的估算各自给一个**开源参考**（不是 r08 那种凭空区间）：参考实现 LOC + 已综合的同类 OOC 数据。
- 把"35T 余量"从"L4 数字 vs 35T"改写为"**预估完整版 vs 35T**"，给出诚实的占用率（红方反算 ~24% LUT，不是 10.39%）。

**判定**：🟥 **待补**。结论方向（35T 够用）成立，但当前的 89% 余量是骨架占用率，不是 Stage-3 完整版的真实余量。

---

### A3〔🟥 P1〕8.5 BRAM 已 17%，trace 大 buffer 从哪儿来？

**问题**：r02/r05 反复确认的事实——抗 PC 端 hiccup 必须有"DMA/出口下游" 的缓冲。本设计：
- 没有板载 HyperRAM（ORBTrace 用 8MB）。
- 板载 4Gbit DDR3 在板子上，**但当前 RTL 完全没接 DDR3 controller**（trace_probe_top 里只字未提）。
- BRAM 已经吃了 8.5 块，35T 总共 50 块（含 RAMB18 等效 100 个 18Kb），剩约 41.5 块 ≈ **~150 KB**。

**反算**：F429@168MHz 满速平均 ~32 MB/s。Linux 主机一次调度抖动 1ms 起，TCP/UDP socket 收流偶发 stall 几十 ms 不奇怪。**150KB / 32MB/s = ~4.7 ms 缓冲窗口**。这够吸收一个调度抖动（1ms 量级），但**抗不住典型 Linux IO 抖动峰值（10–100ms）**。

**对比 ORBTrace**：8MB HyperRAM @ 32MB/s = **250 ms 缓冲窗口**，比当前 BRAM-only 设计高 **50 倍**。

**结论**：要么
- 接 DDR3（要写 MIG controller，~1,500 LUT，且加 ~200~500 ns 的 DMA 延迟，需 PL 侧再加几 KB 小 FIFO 抗 DMA 服务间隙，这条路是 r02 反复说的"缓冲必须在 DMA 上游"，蓝方 r02 时认错了的那条）；
- 要么承认本工具**对 PC 抖动的容忍度只有 ~5ms**，不抗 Linux 大停顿，需要把出口走专用 raw socket 或内核态收流。

**要求蓝方补**：
- 明确 Stage-3 是否接 DDR3。若接，给 MIG controller LUT/BRAM 估 + PL FIFO 深度。若不接，文档明示"缓冲窗口 5ms 量级，不抗主机大停顿"。
- 不能**两边都不说**，"BRAM 17% 了，剩下的够用"是回避问题。

**判定**：🟥 **P1**。不是阻断下单的 P0，但 Stage-3 上板第一周就会撞。

---


---

## B · 时序收敛的可信度

### B1〔🟥 待补证据〕WNS=+1.151 ns 是哪个 corner？

**问题**：Vivado 默认 `report_timing_summary` 报的是当前激活 corner（通常 Slow，但取决于实现命令是否启用了 multi-corner）。蓝方文档没写 corner，commit message 也没记。

**要求蓝方补**：
- 显式跑 `report_timing_summary -corner Slow` 与 `-corner Fast`，分别给 WNS/TNS/WHS/THS。
- 确认 -2 速度等级的工业温度模型是否启用（A7-Lite 用的是 `xc7a35tfgg484-2`，不是 `-2I`，这是**商业级温度（0–85℃）不是工业级**——commit `cc3aa85` 写"工业级"，但 part 名 `-2` 是商业级，**这里有口径不一致**）。
- 若目标使用环境含工业温度，必须用 `-2I` 重综合（资源数字基本不变，但时序会更紧）。

**判定**：🟥 **待补**。1.151 ns 余量看似宽裕，但若是 Fast corner 的数字，hold 才是真问题；若不是工业温度，温漂下的真实 margin 会缩水。

### B2〔🟥🟥 P0-2〕`set_clock_groups -asynchronous` + 2-FF + 多 bit 数据 = CDC bug

**问题**：`trace_probe_top.v` L189-194：

```verilog
reg fr_avail_meta, fr_avail_sync, fr_avail_d;
always @(posedge clk100) {fr_avail_d, fr_avail_sync, fr_avail_meta} <= {fr_avail_sync, fr_avail_meta, fr_avail};
wire fr_pulse = fr_avail_sync ^ fr_avail_d;

reg [127:0] frame_lat;
always @(posedge clk100) if (fr_pulse) frame_lat <= frame128;
```

**这是一个已知的 CDC 错误模式：**
1. `fr_avail` 是 `traceIF` 的 toggle 信号（每帧翻一次），用 2-FF 同步到 clk100，OK。
2. 在 clk100 上检测 toggle 沿（`fr_pulse`），用它**采样 frame128（128 bit 跨域数据）**。

**Bug 在哪**：`fr_avail` 在 trace_clk 上翻转的**那一拍**，traceIF 把新帧写进 `Frame[127:0]`。`frame128` 这 128 根线**在两个时钟域之间是异步的**，从 trace_clk 翻转到稳定下来，每根线的传输延迟有差。`fr_pulse` 在 clk100 上检测到之后**最快下个 clk100 沿就采 frame128**——但此时 frame128 的 128 bit **不能保证全部稳定**（部分 bit 还在传输中），会采到**部分新部分旧的混合帧**。

**xsim 跑通的原因**：
- testbench 里 trace_clk 与 clk100 用了固定的相位关系（仿真器的 0 ps 抖动）；
- `fr_avail` 翻转后所有 128 bit 在仿真里**同一 ps 出现**，没有真实硬件的 wire delay；
- 上板时 LUT 路径延迟 + IO 路径延迟 + 时钟延迟会让 128 bit 不再同时到。

**为什么没被时序工具抓到**：`set_clock_groups -asynchronous` 把 trace_clk 与 clk100 设为异步组——**Vivado 完全跳过这两组之间的 setup/hold 检查**。这条约束的语义是"我自己保证用 CDC 同步器处理"，但**蓝方只对 1 bit `fr_avail` 加了同步器，128 bit `frame128` 是裸跨域**。

**蓝方在文档里这么写**：

> trace_clk → clk100 的 CDC 是简化 2-FF 同步器（生产代码应改 AsyncFIFO）

**这不是"留 Stage-3 优化"，这是当前 RTL 在功能上有 bug**：上板会**随机吞帧/产生坏帧**，且因为 `set_clock_groups` 屏蔽了静态时序检查，**Vivado 不会报警告**，**xsim 也复现不了**。

**正确做法**（必须 Stage-3 上板前修，不是上板后再说）：
- 用 `AsyncFIFO`（amaranth 现成有 `stream.AsyncFIFO`，或写一个 Gray 码指针的）传 frame128 + valid，valid 做协议握手；
- 或者把 frame128 在 trace_clk 域先寄存进 BRAM，clk100 域读 BRAM——和 traceIF 上游的现有 trace_fifo 一致（这正是 orbtrace upstream `core.py` 用 `AsyncFIFOBuffered(tpiu.TPIURawFrame, 4)` 的原因）。

**为什么是 P0 而不是 P1**：上板调试如果出现"偶发坏帧"，第一反应会去查 trace 信号完整性、IDELAY 抽头、PHY delay——而**根因在这条 RTL CDC，不在硬件**。会浪费一周排错时间，且在 hardware 层永远调不出来。

**要求蓝方修**：上板前把 frame128 这条跨域改成 AsyncFIFO，**不是 Stage-3 再说**。资源代价 r08 反算 +300 LUT/+1 BRAM，35T 完全装得下。

**判定**：🟥🟥 **P0 当前已 broken**。现在的 RTL 跑不出可信结果。

### B3〔🟥🟥 P0-1〕RGMII TX 90° 移相 + PHY 端 default delay = 双延迟撞车

**问题**：`fpga_core` 来自 verilog-ethernet 的 NexysVideo example。NexysVideo 板的 PHY（Realtek RTL8211E-VL）**strap 设置 RX/TX delay 都关闭**（PHYRSTB 对应 strap），所以 NexysVideo 的 RTL 在 FPGA 端补：
- RX：`IDELAYE2` 给 RX 数据加 ~2ns delay 对齐（`eth.xdc` 的 `IDELAY_VALUE 0` 是占位，运行时跑 `generate_bit_iodelay.tcl` 调）；
- TX：MMCM 出 `clk125_90`（90° 移相）作为 RGMII TX 时钟，让数据中心对齐到时钟边沿。

**A7-Lite 上的 RTL8211 默认行为**：
- 微相 A7-Lite 用的 PHY 是 RTL8211（commit `cc3aa85` 描述"螃蟹 logo"）。RTL8211E 的 strap pin（RXD0/RXD1/RXD2/RXD3/RXER）默认配置在大多数板厂的设计里是**RX delay 开 + TX delay 开**（RGMII 1.0 兼容模式），这是行业惯例（Linksys/TP-Link 类设计大多这样）。
- 微相板没公开 strap 接线图（A7-Lite_Rev1_3.pdf 我没看到 strap 详细信息，蓝方的 T5 核对里也没列）。

**如果 PHY 端 delay 已开 + FPGA 端再加 90° 移相**：
- TX：FPGA 出的 90° 移相数据 + PHY 内部又加 ~2ns → **数据被推过时钟边沿**，PHY 收不到正确包；
- RX：FPGA 加 IDELAY ~2ns + PHY 已经出 delayed 数据 → **过早采样**，bit error 暴涨。

**这是上板第一刀就会撞的坑**，且 OOC/post-impl 时序**完全看不到**——Vivado 不知道 PHY 内部行为，时序里 RGMII 是按 spec 打分的。

**蓝方在 T5 选板门里的相关条目**：

> Ethernet: 确认 RGMII（Realtek PHY），匹配 T1 MAC 选择

**这只确认了"协议是 RGMII"，没确认 strap 配置。** 这是 r08 红方就该问、当时没问的——本轮补上。

**要求蓝方补**：
- 翻 A7-Lite 原理图，**找到 RTL8211 的 5 个 RGMII strap pin（RXD0–3 + RXER 或 LED 复用脚）的上电默认电平**，确定 RX/TX delay mode（关 / RX 开 / TX 开 / 双开）。
- 根据 strap 默认值，**确认 fpga_core 的 RGMII 处理是否兼容**：
  - 若 PHY strap = RX/TX 都关（NexysVideo 同款）→ 直接复用 fpga_core，OK。
  - 若 PHY strap = RX/TX 都开 → **FPGA 端必须改**：去掉 90° 移相、IDELAY 设为 0、用同步 RGMII（rgmii_txc = clk125 直接驱动）。
  - 若 strap 不可控（无外部上拉/下拉）→ 上电后用 MDIO 配置 PHY register（RTL8211E 的 Page 0xa43 寄存器 0x0d 控制 RGMII delay），但**当前 trace_probe_top.v 的 phy_mdio = 1'bz / phy_mdc = 1'b0**（MDIO 完全未驱动）——意味着 PHY 只能用 strap 默认值。
- **下单买板前**就要拿到 strap 信息，因为如果 strap 不利，可能要选**带 strap 跳线**的板，或者干脆换板。

**判定**：🟥🟥 **P0 上板前必须解决**。这条不修，Stage-3 第一天网络就不通，且会被误诊为 trace 信号完整性问题。

### B4〔🟥 P1〕`phy_reset_n` 由 fpga_core 驱动，但 PHY 上电稳定时序未审视

**核对结果**（与蓝方报告对照）：蓝方 commit message 里写"phy_rst_n hard-coded 1'b1"，**但实际 RTL 不是这样**——`trace_probe_top.v` L263 把 `phy_reset_n` 直接接到了 `fpga_core` 的同名输出，由 fpga_core 内部驱动。这是好事，但带来新问题：

- **fpga_core 驱动 phy_reset_n 的逻辑是什么？** 简单复位计数器还是和 `clk125`/`clk_mmcm_lock` 联动？
- **RTL8211 datasheet** 要求 `RESET_N` 拉高后等 `30 ms`（典型）让 PLL 锁定 + RX clock 稳定。fpga_core 的 reset 释放 → PHY ready 这段时序未审视。
- 如果 fpga_core 在 PHY 还没出 clkout 之前就开始尝试发包，**phy_rx_clk 不存在 → fpga_core 整个 RX 路径死锁**（输入时钟没沿，时钟域里所有 reg 静止）。

**要求蓝方补**：
- 翻 `fpga_core.v` 看 phy_reset_n 的驱动逻辑，确认是否符合 RTL8211 的 30ms 复位时序要求。
- 若不符合，加一个 reset stretch（25M 周期 @ 50MHz = 0.5s 稳）。
- 蓝方 commit message 与 RTL 不一致，**修正 commit 描述**。

**判定**：🟥 **P1**。上板前补一下，避免 Stage-3 第一周排错。

---


---

## C · xsim 仿真覆盖率

### C1〔🟥 P1〕xsim 是回归测试的下沿，不是上沿

**蓝方仿真做了**：1 frame，理想对齐，IDELAY tap = 0，IDELAYCTRL ready 后才发数据，sync 一次成功。

**红方独立反算**——上板必然遇到、xsim 没覆盖的场景：

1. **IDELAYCTRL 未 ready 期间的输入**：上板时 trace 信号可能在 FPGA 上电瞬间就来（target 已经在跑），FPGA 这边 IDELAYCTRL 还没 ready。当前 `trace_capture_a7` 的 `idelayctrl_rdy` 只用作上层 `tif_rst` 的一个分量——但 ready 之前的 trace 数据**已经进入 IDDR 路径并出 trace_a/trace_b**，traceIF.rst 拉高时这些噪声已经在路径上了。需要验证：rst 释放后第一个 sync 字（0x7fffffff）能否正确建立 sync。
2. **IDELAY tap 非 0**：testbench 强制 tap=0，但实际 deskew 训练后 tap 会扫到 5–25 之间。**IDDR 的 SAME_EDGE_PIPELINED 模式在 IDELAY 加非零延迟后，Q1/Q2 相对 C 的相位关系如何？** 这是 7-series IDDR + IDELAYE2 组合的已知细节，需要仿真覆盖。
3. **稍微偏中心采样**：把 testbench 的 trace_clk vs trace_data 关系移 ±10ps，看 traceIF 是否还能正确组帧。这是源同步采样最薄弱的点。
4. **连续 1000 帧**：当前只验 1 帧。orbtrace upstream 的 `verilog/testbeds/traceIF_tb.v` 只跑 ~10 frame，**蓝方继承的 testbench 也是这个数量级**。需要扩到 1000+ frame，看是否有累积错误（COBS 计数饱和、checksum 累加溢出、super_framer interval 边界）。
5. **重同步场景**：sync 丢失 → 进入 unknown → 再次 sync。upstream `traceIF_tb_resync.v` 已覆盖（蓝方 commit `bc3335f`），但**未集成到 trace_probe_top 顶层 sim**——`trace_capture_a7_tb.v` 只跑一次成功 sync。

**要求蓝方补**：
- 把 `trace_capture_a7_tb.v` 扩到至少覆盖：tap=0/8/16/24 四档、相位 ±10ps 抖动、1000 frame、一次 sync 丢失再恢复。
- 加一个"上电瞬间 trace 就在跑"的场景：driver 在 IDELAYCTRL ready 之前 30µs 就开始翻转，看 ready 后 sync 是否在 100 帧内建立。

**判定**：🟥 **P1**。当前 xsim 是冒烟测试级别，不是 Stage-2 完成判据级别。

### C2〔🟨 待求证〕IDDR SAME_EDGE_PIPELINED vs ECP5 IDDRX1F 等价性

**问题**：蓝方在 trace_capture_a7.v 注释里写"和 litex DDRInput 在 7-series 的 IDDR 一一对应"——这是对的，litex 确实把 ECP5 的 DDRInput lower 成 ECP5 的 IDDRX1F、把 7-series 的 lower 成 IDDR。但**两者的"瞬时相位"不同**：

- **ECP5 IDDRX1F**：rising 边沿采到的 D0 与 falling 边沿采到的 D1，**在 falling 之后的 rising 同时输出**（一拍延迟，但两个 sample 同沿出）。
- **7-series IDDR `SAME_EDGE_PIPELINED`**：D1（rising）和 D2（falling）**都在下一个 rising 边沿出**——但相对原始 D 输入，**整体晚了一拍**（pipelined 的代价）。

**这一拍延迟会不会让 traceIF 的 sync 检测/帧对齐位移**？

**反算**：traceIF 是 `always @(posedge traceClkin)`，靠 trace_a/trace_b 的双沿输入做 16-bit 移位。无论 IDDR 内部相位关系如何，traceIF 看到的是"每个 trace_clk 上升沿 trace_a + trace_b 各 4 bit"——**只要两个 sample 同时出现在同一个 trace_clk 周期，就 OK**。

但是！**IDDR pipelined 模式下，trace_a/trace_b 相对 D 输入晚了一拍，相对 trace_clk 仍是同步出。这等于在 trace_clk 域内整体延迟了一拍——traceIF 状态机看到的 "第 N 个 trace_clk 周期的数据" 实际是 D 输入在 "第 N-1 个周期" 的样本。**

**这本身不是 bug**（traceIF 不在乎"绝对时刻"，只在乎"连续性"），但**会让 sync detect 和 deskew 训练的"窗口位置"整体偏移一拍**。

**要求蓝方补**：
- 跑一次 `DDR_CLK_EDGE = "OPPOSITE_EDGE"` 的对比仿真，看 sync 是否仍能建立（应该能）；
- 在文档里明确：trace_a/trace_b 相对 trace_clk 有一拍 IDDR pipeline 延迟，deskew 校准的"零点"会差一个 UI——上板调试时第一次扫 IDELAY 抽头，发现"窗口中心在 tap 24 而不是 tap 16"不要慌，是这一拍的偏移；
- 或者改用 `DDR_CLK_EDGE = "SAME_EDGE"`（无 pipeline，更对应 ECP5 IDDRX1F），代价是 routing 紧一点。

**判定**：🟨 **存疑**。不影响功能，但影响"上板第一次抽头扫描的预期窗口位置"，文档里得写清楚，避免误诊。

---

## D · T5 选板门残留疑点

### D1〔🟥 待补依据〕trace 数据 4 线 F13/F14/E13/E14 的 bank 与 skew 未确认

**核对结果**：
- xdc 把 `trace_data_in[0..3]` 分配到 `F13 / F14 / E13 / E14`。
- 蓝方报告 §T5 写"GPIO1 全在 Bank 16"——这是**整体 Bank 性质**的陈述，**没有逐个引脚核对**。
- 红方反查 `xc7a35tfgg484-2` 器件文档：F13/F14/E13/E14 中，**只有 D17 一个被蓝方明确说是 Bank 16 的 MRCC（`IO_L12P_T1_MRCC_16`），剩下 4 个数据脚的 bank 归属和 IO 类型蓝方没列**。

**为什么这是问题**：
- 7-series 的 IDELAYE2 必须**与 IDELAYCTRL 同 bank**才能用同一个 IDELAYCTRL 实例驱动；
- 跨 bank 需要**第二个 IDELAYCTRL**（占一个 bank 的 IDELAYCTRL primitive）；
- 蓝方当前只实例化了**一个 IDELAYCTRL**——若 trace 数据脚跨 bank，**OOC 综合可能通过（IDELAY-IDELAYCTRL 关系是 implementation 阶段才解析），但 placement 会失败或被强制就近用了一个不存在的 IDELAYCTRL 的 dummy**。

**要求蓝方补**（零成本，Vivado device library 查表即可）：
- 在 Vivado 里 `get_property BANK [get_package_pins F13]` 等，逐个确认 4 个 trace 数据脚都在 Bank 16；
- 若有跨 bank 的，重新分配引脚（GPIO1 整组应该都在 Bank 16，但需要确认）；
- 如果**真的**有跨 bank 不可避免，加第二个 IDELAYCTRL 实例。

**PCB skew**：A7-Lite 的 GPIO1 引出方式是差分对（蓝方 T5 已确认），但 5 根 trace 线（TRACECLK + 4×TRACED）**走的是非差分模式**——它们是否在 PCB 上等长？skew < 100ps 的要求板厂通常没保证（GPIO1 是通用扩展口，不是 SI 受控走线）。

**判定**：🟥 **待补 datasheet 核对 + skew 估算**。下单买板前必查。

### D2〔🟥 P0〕35T/100T 引脚兼容性"待厂商确认"是上板前的硬门，不是软门

**蓝方报告 T5**：

> 35T 与 100T 引脚兼容性 ⚠️ 待确认

**这条不能"挂着"进 Stage-3**。如果买了 35T 后发现资源不够要换 100T，**约束/PCB 不兼容则换板等于重做**。FGG484 同封装通常引脚兼容，但 7-series 内部不同 die 的 IO bank 组成有时会差。

**要求蓝方在下单前必须**：
- 拿到微相书面回应（邮件/客服截图，留档）："xc7a35tfgg484 与 xc7a100tfgg484 在本板（A7-Lite Rev1.3）上引脚映射 100% 兼容，可直接互换"；
- 或 cross-check Xilinx UG475 的 FGG484 pinout 文档，逐个确认 GPIO1 用到的引脚在 35T 和 100T 上都是同型号（IO bank、IO 类型、是否 MRCC）。

**判定**：🟥 **P0 不可挂着进 Stage-3**。

### D3〔🟥 P1〕`create_clock -period 10.000 -name trace_clk_in` = 100MHz 假设乐观

**问题**：xdc 把 trace_clk 约束为 100MHz。但**STM32F429 的 TPIU TRACECLK 实际频率**：
- TRACECLK = HCLK / TPIU.ACPR；
- F429@168MHz HCLK，ACPR=1（最快）→ TRACECLK = **84 MHz**；
- ACPR=0（最快设置不同 SoC 不同）→ TRACECLK 可达 168MHz。

**ARM TPIU spec 明确 trace port 上限 ≈ 200 MHz**（DDR 实际净 4-bit @ 400Mbps），实际 STM32 上**最快 TRACECLK 通常是 84MHz 或 100MHz** 取决于配置。

**蓝方约束 10ns（100MHz）是合理上沿**，但：
- 若上板实际跑 84MHz，xdc 的 100MHz 约束**比真实严**——WNS 数据**比上板乐观**；
- 若 ACPR=0 设置出 168MHz TRACECLK，**当前 100MHz 约束完全错**，post-impl 时序数字无效；
- 当前 xdc **没有写 TRACECLK 的 input delay 约束**——`set_input_delay -clock trace_clk_in -max ... [get_ports trace_data_in]`，缺这条 Vivado 不知道 source-synchronous 的 setup/hold 窗口在哪。

**当前 WNS=+1.151 ns 对采样链路的有效性存疑**。

**要求蓝方补**：
- 确定 STM32F429 实际 TRACECLK 配置（PoC-A 阶段实测）；
- 加 `set_input_delay -clock trace_clk_in -max 0.5 [get_ports {trace_data_in[*]}]`、`set_input_delay -clock trace_clk_in -min -0.5 [get_ports {trace_data_in[*]}]`（按 source-synchronous 中心对齐 + ±UI/4 窗口）；
- 用真实 TRACECLK 频率重跑 timing。

**判定**：🟥 **P1**。当前的 1.151ns WNS 对其他逻辑（clk100 域）有效，但**对 trace_clk_in→IDDR 这条最关键的源同步路径无效**（缺 input_delay 约束）。

---


---

## E · 流程性疑点

### E1〔🟥 P1〕两套 IDELAY 用户共用同一个 IDELAYCTRL？

**问题**：当前设计同时用 IDELAY 在两处：
1. **trace_capture_a7**：4 个 IDELAYE2 用于 TRACED0-3 deskew，IDELAYCTRL 实例在 trace_capture_a7 内部，喂 200MHz；
2. **fpga_core (verilog-ethernet 的 NexysVideo `fpga.v`)**：4 个 IDELAYE2 用于 phy_rxd[0-3] + 1 个用于 phy_rx_ctl，**还有一个独立的 IDELAYCTRL 实例**喂同样 200MHz。

但 NexysVideo example 的 `fpga.v` 是**板级 wrapper**，**蓝方实际综合的是 `fpga_core.v`**（MAC 部分）——而 NexysVideo example 的 RGMII 输入侧的 IDELAY/IDELAYCTRL 是在 `fpga.v` 里，不在 `fpga_core.v` 里！

**红方实读 verilog-ethernet 仓库**确认：
- `example/NexysVideo/fpga/rtl/fpga.v` L228+ 实例化 `IDELAYCTRL idelayctrl_inst` 和 4 个 `IDELAYE2 phy_rxd_idelay_*`；
- `fpga_core.v` 不含 IDELAY，它假设输入 phy_rxd 已经被上层 delay 过；
- **蓝方的 trace_probe_top.v 直接接 fpga_core，跳过了 fpga.v——所以 phy_rxd 没有 IDELAY**。

**这意味着两件事**：
1. **当前 RGMII RX 路径少了 IDELAY**（如果 PHY 端 RX delay 关闭，RGMII 时序就崩）——和 B3 的 strap 问题耦合；
2. **资源数字 1,871 LUT 不含 RGMII RX IDELAY**——但因为 IDELAY 是 IO 硬核，本身 LUT=0，所以**资源结论不变**；
3. **但若按 NexysVideo 同款补全 RGMII IDELAY，需要**：① 第二个 IDELAYCTRL（同 bank 与 RX phy，可能跨 bank → 第三个）；② 4 个 phy_rxd IDELAYE2 + 1 个 phy_rx_ctl IDELAYE2（IO 硬核，资源不变）。

**要求蓝方补**：
- 把 NexysVideo `fpga.v` 里的 RGMII RX IDELAY/IDELAYCTRL 部分搬到 `trace_probe_top.v`，**或确认 PHY 端 RX delay 已开（B3）从而不需要 FPGA 端 IDELAY**——二者只能选一；
- 重新综合，看新增 IDELAYCTRL 是否引入额外 BUFG / 跨 bank 问题。

**判定**：🟥 **P1**。当前数字漂亮一部分原因是 RGMII RX 处理不完整。

### E2〔🟥 P1〕vendor demo 验证 PHY 活性 → verilog-ethernet 移植，无 transitive 证明

**蓝方建议**（从 Stage-3 plan 推测）：先烧厂商 demo 的 ICMP ping example 验 PHY 物理层 OK，再合 trace。

**问题**：vendor demo（微相 A7-Lite 自带的）用的是**vendor 自己的 RGMII 处理 + UDP/IP stack**（通常是 LWIP 或类似的 GMII MAC + soft stack）。它跑通**只能证明**：
1. PHY 上电正常、time clkout 工作；
2. 物理层（线序、电气）OK；
3. PHY strap 配置下能跑 100M 或 1G（取决于 demo）。

**它不能证明**：
1. verilog-ethernet 的 `eth_mac_1g_rgmii` 与 A7-Lite PHY 的 strap delay mode 兼容；
2. fpga_core 的 90° 移相对应 PHY 的延迟模式正确；
3. UDP/IP/ARP 栈的实现兼容主机网络栈。

**这是 r02 红方就在质疑 Zynq 时点过的同类错误**：用 A 的成功推 B 的成功，没有 transitive 证明力。

**要求蓝方明确**：
- vendor demo 跑通后，**还要用 verilog-ethernet 的 NexysVideo example（不是 fpga_core，是完整的 fpga.v + fpga_core.v + 板级 IDELAY 配置）烧到 A7-Lite，看能否 ping 通**；
- 如果能 → 证明"verilog-ethernet 与本板 PHY 兼容"；
- 如果不能 → 必须根据 PHY strap 改 RGMII 配置（B3）。

**判定**：🟥 **P1**。Stage-3 路线图要写清楚，不能跳。

### E3〔🟥🟥 P0〕CDC 简化是 P0 bug，已在 B2 详述

**蓝方在文档里把这条列为"诚实声明"**：

> trace_clk → clk100 的 CDC 是简化 2-FF 同步器（生产代码应改 AsyncFIFO）

**但这不是"留 Stage-3 优化"**——它是当前 RTL 的功能 bug。诚实声明承认的是"实现简化了"，**但隐瞒了"这种简化在多 bit 跨域 + 非握手协议下，会产生随机坏帧"** 这个事实。

判定见 B2，重申：🟥🟥 **P0 当前已 broken，上板前必修**。

---

## 新增风险点（蓝方未识别）

### N1〔🟥 P1〕`opt_design` 默认 directive 下的 trace 流水线被剪枝风险

详见 A1。蓝方用 `sw[]` 假喂的设计，让 Vivado 有理由把 trace 流水线判定为 dead 或 pseudo-dead 部分剪掉。当前 2,161 LUT vs OOC 累加 2,365 LUT 的差有可能就是被剪的部分。

### N2〔🟥 P1〕`sys_rst` 同步释放但 IDELAYCTRL.RST 是异步释放

`trace_capture_a7.v` L62 直接把 `rst` 喂给 `IDELAYCTRL.RST`：

```verilog
IDELAYCTRL u_idelayctrl (
    .RDY    (idelayctrl_rdy),
    .REFCLK (ref_200m),
    .RST    (rst)
);
```

但 `rst = ~rst_n` 来自外部按钮，**没经过 ref_200m 的同步**。Xilinx UG471 / DS181 明确：IDELAYCTRL.RST 必须**异步置位、同步释放到 REFCLK（这里是 ref_200m），且释放前最少持续 60ns**。

当前实现：
- 异步置位 ✅；
- **同步释放 ❌**——`rst_n` 释放时刻与 ref_200m 关系不确定；
- 持续时长 ✅（按钮按下时间 >> 60ns）。

**结果**：上电时 IDELAYCTRL 可能不进入正确的 ready 状态，`idelayctrl_rdy` 永远 stay low，整个 trace 链路死锁。这是上板第一周会撞、且不容易诊断（信号在 FPGA 内部）的坑。

**要求**：在 `trace_capture_a7` 内部加一个对 ref_200m 同步的复位释放逻辑。资源 ~10 LUT。

### N3〔🟨 存疑〕`u_bufg_clk` 直接 BUFG 化 trace_clk_ibuf——满速后会成为时序瓶颈

蓝方当前用 BUFG 把 TRACECLK 全局化（trace_capture_a7.v L70）。BUFG 适合 OOC 与低速验证，但：
- BUFG 网络延迟较大（4-6 ns），且**全器件单一时钟域**——TRACED0-3 的 IDDR 与 trace_clk 的相对 path delay 受 BUFG 影响；
- 满速（100MHz trace_clk = 10ns 周期）时，BUFG 引入的 jitter + skew 会**吃掉源同步窗口**；
- 正确做法是 **BUFR + BUFIO + 区域约束**（Region Clock + IO Clock），让 trace_clk 只在 IDDR 所在的 IO bank 内传播，skew 最小。

**蓝方在 trace_capture_a7.v 注释里已自白**：

> For real HW BUFR/BUFIO + region constraints can give better source-synchronous timing; BUFG is OOC-friendly and conservative.

**红方裁定**：作为 OOC 阶段 OK，**但 Stage-3 上板前必须改成 BUFR/BUFIO**。这条要写进 Stage-3 必做项，不要在上板调试满速时才发现"为什么 IDELAY 扫了一圈都没有正余量"——根因可能是 BUFG。

---

## Stage-3 上板首次开机 checklist（红方视角硬门）

按时间顺序、逐项硬门，每条不达标不进下一条：

1. **C-1 板厂客服书面确认** 35T/100T 在 A7-Lite Rev1.3 上 FGG484 引脚 100% 兼容（D2）。
2. **C-2 PHY strap 确认**：A7-Lite 原理图核出 RTL8211 5 个 strap pin 默认值，确定 RX/TX delay mode（B3）。
3. **C-3 修 P0-2 CDC**：trace_clk → clk100 的 frame128 改 AsyncFIFO（B2/E3），重综合 + xsim 回归。
4. **C-4 修 P0-1 RGMII delay**：根据 C-2 的 strap 结论，决定保留 / 删除 fpga_core 的 90° 移相 + IDELAY（B3）。
5. **C-5 加 RGMII RX IDELAY/IDELAYCTRL**（如果 C-2 决定 PHY 端 delay 关闭，必加；E1）。
6. **C-6 加 trace_data_in 的 set_input_delay 约束**，按 STM32F429 实测的 TRACECLK 频率重综合（D3）。
7. **C-7 修 IDELAYCTRL.RST 同步释放**（N2）。
8. **C-8 trace_clk 改 BUFR/BUFIO 区域时钟**（N3）。
9. **C-9 烧 verilog-ethernet 完整 NexysVideo example（含 fpga.v）到 A7-Lite，验 ICMP ping 通**（E2）——这一关不通，trace 部分免谈。
10. **C-10 烧本设计，先验**：① ILA 看 idelayctrl_rdy=1；② STM32 不发 trace 时，IDELAY 抽头扫 0–31 看 noise floor；③ STM32 发 trace 时，IDELAY 抽头扫眼图 → 找 ≥8 tap 的 valid window（眼高、setup/hold 余量按 r07 终审 checklist 阈值）。
11. **C-11 端到端**：STM32 跑已知 program（hello world + 固定 loop）→ trace → verilog-ethernet UDP → PC orbuculum 解出函数跳转，与 .elf 反汇编对照，逐条一致。

---

## 可下单条件

**红方裁定：当前不可下单。**

下单 35T 的最小补全集（按优先级）：

### 必补（不补不下单）
- ✅ **拿到 P0-2 CDC 修复后的重综合数据**：frame128 用 AsyncFIFO 跨域，新 utilization + 新 timing report（B2）。
- ✅ **拿到 PHY strap 信息**：A7-Lite 原理图 + RTL8211 strap 分析，确定 delay mode（B3）。
- ✅ **拿到 35T/100T 引脚兼容书面确认**（D2）。
- ✅ **拿到 `report_design_analysis -hierarchical` 证据**，证明 trace 流水线没被剪枝（A1）。
- ✅ **加 `set_input_delay` 约束并按真实 TRACECLK 频率重跑时序**（D3）。

### 强烈建议补（补完更稳）
- 🟨 4 个 trace 数据脚的 bank 逐个核对（D1）。
- 🟨 IDELAYCTRL.RST 同步释放修复（N2）。
- 🟨 xsim 覆盖率扩到 1000 帧 + tap 非零 + 相位抖动（C1）。
- 🟨 完整版资源预估（含 deskew/AsyncFIFO/UDP 桥）（A2）。
- 🟨 BRAM 预算与 DDR3/PC 抖动容忍度声明（A3）。

### 关于 35T vs 100T

**红方意见：35T 仍可锁定，但理由要换。**

蓝方现在的理由："实测 10.39%，剩 89% 余量"——这个数字含两类乐观（trace 可能被剪 + 完整版未估算），不能照搬。

**正确理由**：r08 红方反算的"完整版 ~24% LUT"在 35T 上仍宽裕，且 100T 仅多 4 倍 LUT、对当前用例无帮助、价格高。**只要 P0-1/P0-2/D2 三个硬门补完，35T 仍是合理选择。** 但若任一硬门发现致命阻塞（如 PHY strap 冲突且无法 MDIO 修复 + 板厂回复 35T/100T 不兼容），需重新评估是否换板。

---

## 一句话总评

**蓝方拿到了 OOC/post-impl 真实数字，这是六轮里最大的进步——但读 RTL 后发现两个 P0 bug（多 bit 跨域裸传 + RGMII delay 双计），一个数据失真根因（trace 流水线可能被剪），加上 PHY strap 与引脚兼容这两个零成本就该闭环却挂着的硬门。"35T 锁定下单"不是凭一份漂亮 utilization report 能拍板的事——把上面 5 条必补补完，再来下单。**
