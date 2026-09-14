# 21 — NACK 重传上板集成设计计划（doc 19 §14.5 展开）

**日期**：2026-09-04
**前置**：doc 19（架构 + P1-P4 离线验证）、`rtl/la_ddr_ring_streamer.v`（已合入）、
`rtl/la_ddr_writer.v`（已存但未在 top 实例化过）、`rtl/ddr3/*`（既有 vendor 通路）、
`scripts/nack_{protocol,rx,loopback_test}.py`（PC 端已完成）。
**范围**：只谈上板集成落地方案，不重复 doc 19 的架构论述。

---

## 1. 为什么单独写这份

doc 19 覆盖了：
- 三层可靠传输架构（L1 降速 / L2 DDR 环 / L3 NACK 重传）
- seq↔DDR 地址绑定、能力边界（NACK-fail 诚实置位）
- P1-P4 分阶段 + 离线仿真验证（4/4 ALL_PASS）

doc 19 §14.5 明确列出**下一步**：
> 尚未上板：待综合 `la_ddr_ring_streamer` 进 top（接 `fpga_core_net` self-TX +
> CTRL :5002 NACK 解析），跑真实注入丢包的端到端 P4。

本文档展开这一步的**具体集成设计**——RTL 接线图、模块修改清单、综合/时序策略、
每一步的上板验收判据。不覆盖 doc 19 已经论过的架构决定。

---

## 2. 当前 top 现状与目标形态

### 2.1 现状（`trace_stream_top.v` STREAM=1 分支，doc 19 前）

```
cap_byte (clk200) ─► axis_async_fifo(clk200→clk125) ─► seq+payload 打包 FSM
                                                        ─► fpga_core_net UDP self-TX :5555
```

**没有 DDR** 在数据通路里。`la_ddr_writer.v` 存在于树中，但**没有任何顶层实例化**。
DDR3 controller / arbiter 已在 `rtl/ddr3/` 就位，但当前 STREAM 路径完全绕开。

### 2.2 目标形态（doc 19 三层）

```
cap_byte (clk200) ──► la_ddr_writer ─► DDR3 ring
                          │
                          │ wr_ptr_words / wr_words_committed
                          ▼
                    la_ddr_ring_streamer ─► seq+payload+rtx 包组装
                          ▲                    │
                          │                    ▼
        fpga_core_net UDP RX :5002 ──► NACK   fpga_core_net UDP TX :5555
        (PC → FPGA)          解析              (FPGA → PC，含常态 + 重传)
```

**关键**：数据通路整体前移一站——原来"cap_byte → axis_async_fifo → 打包 → 网络"，
现在"cap_byte → DDR ring → streamer → 打包 → 网络"。streamer 已经内建 clk200→clk125
CDC（161-bit async FIFO 携带 rtx+seq+data），所以**旧的 axis_async_fifo 直接删掉**，
不留过渡态。

---

## 3. 集成分五步，每步单独可上板可验证

**决策原则**：绝不一次改完综合上板。每一步产生一个**独立的 .bit** + **单独的验收判据**，
坏了能立刻回退。**S1 先剥离 trace 链路，自造可预知流量压测 DDR+网络子系统**——
分离 trace 采集侧 bug 和传输侧 bug，等 S1 全绿再接真 trace 源。**S1 再拆两小步：
S1a 复现厂商 DDR loop 压测证 DDR 硬件+MIG IP+ddr3_ctrl 全通路正常；S1b 才加
writer/streamer/网络三层。任一失败精确定位到该层。**

| 步 | 数据源 | 校验位置 | RTL 变化 | 产物 bit | 验收判据（不满足 = 回退） |
|---|---|---|---|---|---|
| **S1a** | **FPGA 内部 counter（`ddr3_generate_data.v`）** | **FPGA 内部** `error` 信号 + ILA | 综合厂商 `21_ddr3_test`（**不动 orbtrace 代码**，独立工程） | `ddr3_loop_test.bit`（厂商 demo） | Vivado ILA 抓 `error=0` 持续（写-读-比对 loop 全绿）；MIG cal_done=1；`init_calib_complete=1`。**证 DDR 硬件+MIG IP+ddr3_ctrl 层完好**。 |
| **S1b** | **FPGA 内部 ramp 计数器（la_ddr_writer 输入端）** | **PC 侧** `_rampcheck.py`（monotone-mod-256）| 加 `la_ddr_writer` + `la_ddr_ring_streamer` 到 orbtrace top，writer 输入接 CSR 门控内部 ramp（不接 cap_byte）；streamer 输出替代 `axis_async_fifo` 进 fpga_core_net；NACK tie 0；MIG IP 复用 S1a 生成的 | `ddr_ring_selftest.bit` | 1) `words_written / wr_ptr_words` 单调递增；`wr_lost_bytes=0`<br>2) PC 侧收流 payload 逐字节 = ramp，零破损<br>3) `stream_endurance` 30 min seq-gap=0、`ring_overrun=0`<br>4) 数据率扫（token divider）10→200 MB/s，标定 DDR+网络子系统零丢上限。<br>**STM32 完全不需要参与**。 |
| **S2** | **真 trace（cap_byte）** | 内容 diff 旧 STREAM 直通路径 | writer 输入切换为 `cap_byte`；加 `USE_DDR_RING` generic 默认 0（回退保底），开启走 DDR ring | `trace_iddr_ddrring.bit` | 内容与旧 STREAM 直通路径 byte-identical（同刻两路 diff）；写吞吐追上真 trace（100 MB/s @ 4-bit @ 100 MHz） |
| **S3** | 真 trace + NACK 输入 | PC 端 `nack_rx.py` | 加 UDP RX（:5002）→ 解 NACK → 送 streamer；`stream_rtx` 位喂回打包 FSM 作为包头 flag | `trace_iddr_ddrring_nack.bit` | PC 发合法 NACK，观测 `nack_busy=1` 一段；PC 收到带 rtx 标记的包、内容与原 seq 位置字节一致；越界 NACK → `nack_fail=1` |
| **S4** | 真 trace + 注入丢包 | PC 端 hole=0 | 端到端 P4：`tc netem` 注入丢包 → PC `nack_rx.py` 检测缺口 → NACK → FPGA 重传 → **PC 最终 seq-gap=0** | 同 S3 | 1% 丢包注入 1 小时后，PC 端 hole=0；20% 丢包 seq-gap 集中在超窗口位置且被 `nack_fail` 明确标注 |

上板任一步失败时的降级：
- **S1a 失败**（DDR 硬件/MIG/ddr3_ctrl 有问题）：不推进整个 doc 21，先解决基础设施
- **S1b 失败**（DDR ring writer/streamer/网络路径有问题）：S1a 已证 DDR 好，问题精确到 writer/streamer/fpga_core_net 三层之一，单点定位
- **S2 失败**（真 trace 接 writer 有问题）：S1b 已证 DDR ring + 网络好，差在 cap_byte 接入 writer 的 CDC/时钟约束
- **S3 失败**：S2 的常态 drain 已能替代原 axis_async_fifo，只是没 NACK
- **S4 失败**：S3 已上板，可离线（`nack_loopback_test.py`）验证协议正确性

### 3.1 为什么 S1 剥离 trace（原理与收益）

**问题**：doc 19 §7 的 P0e "并发读写零污染" 是仿真结论（`tb_la_ddr_ring` 4/4 pass），
上板真跑时 DDR ring 的并发读写、wrap、drain 追指针追不上等问题只有硬件才会暴露。
**若同时把真 trace 接进来调，出问题时无法区分**：
- 是 DDR ring 通路本身有 bug（例：writer async FIFO 参数、streamer 追指针边界）
- 还是 cap_byte 输入有抖动（IDDR 相位、TRACECLK 稳定性——doc 20 反复调过的东西）

**剥离方案**：在 `la_ddr_writer` 输入前加一个 `SELFTEST_RAMP` mux，参考现有
`trace_stream_top.v:594-625` 的 `bw_cnt` 做法。当 CSR bit 置位：
```
ramp_byte (clk200 每周期 +1) → la_ddr_writer → DDR3 ring → la_ddr_ring_streamer
                                             → fpga_core_net UDP :5555
```
数据是可预知的 monotone-mod-256 单字节 ramp，**PC 侧 numpy 逐字节校验一步就出零丢/破损**
（`scripts/_rampcheck.py` 已有，doc 20 §12 用过）。

**关键**：从 writer 输入到 PC 收流全通路都在测，且**不依赖任何 trace 侧硬件**——
STM32/DAPLink 可完全断开，只留 FPGA + 网线。

### 3.2 S1 验收后能得到的独立结论

1. **DDR3 controller + writer + streamer 硬件并发能力**——doc 19 §7 P0e 从仿真升级为上板实证
2. **XC7A35T 资源和时序确实容得下**——若 S1 时序过不了或资源爆表，直接暴露，
   不用等 trace 挂了才发现
3. **DDR+网络子系统的实际零丢上限速率**（扫 10→200 MB/s）——比 doc 19 §12 记录的
   `recvmmsg+pin` 上限更细的分层数据，能回答"AX88179 的 115 MB/s 天花板是硬件的
   还是软件的、加了 DDR 缓冲能不能突破"
4. **DDR ring 缓冲能不能兜住 AX88179 静默丢帧**（AGENT.md §2）——直接实测,不用等 NACK 层

这四条结论**独立于 trace 层任何变量**，是纯传输子系统的能力标定，可直接更新 doc 19 §14.5。

### 3.3 S1a 落地（先做，最快）

厂商 `21_ddr3_test` demo（`A7_Lite/04_source_code/A7_lite_demo_35T_new/A7_lite_demo_35T/
21_ddr3_test/`）**开箱即用**：`ddr3_loop_test.v`（顶层）+ `ddr3_generate_data.v`（写-读-比
对状态机）+ `ddr3_ctrl.v`/`ddr3_wr_ctrl.v`/`ddr3_rd_ctrl.v`/`ddr3_arbit.v` + MIG IP + XDC。

**S1a 步骤**（不改任何 orbtrace 代码）：
1. 打开 `ddr3_test.xpr`（Vivado 2021.1）或从零综合
2. 生成 bitstream → `impl_1/ddr3_loop_test.bit`
3. `openFPGALoader -c ft232 --fpga-part xc7a35tfgg484 ddr3_loop_test.bit`
4. Vivado Hardware Manager 挂上 ILA（工程内自带 `ila_top`）
5. 触发条件：`error==1`（应永远不触发）；辅以 `wr_done` 上升沿采样看 `wr_addr` 是否单调
6. 让它跑 5-10 分钟，`error` 保持 0 → S1a ✅

**判据表**：
- MIG `init_calib_complete=1`（DDR 校准通过）
- ILA `error=0` 持续 ≥ 5 分钟
- `wr_addr` / `rd_addr` 单调推进并 wrap（demo 内部循环使用整个 DDR 空间）
- `wr_done` / `rd_done` 交替出现，比例 1:1

**S1a 完成后我们得到**：
- 板上 DDR3 硬件（板载 IC）在 400 MHz 下**零错**
- Vivado 2021.1 + MIG IP + XDC 引脚映射**综合流可用**
- orbtrace 的 `ddr3_ctrl / ddr3_wr_ctrl / ddr3_rd_ctrl / ddr3_arbit`（与 demo 同源）行为一致
- 上板 flow（openFPGALoader + `fpga_health.py` / `ddr_selftest_status.py`）就绪

**⚠️ 便宜路径**：orbtrace 已有 `trace_ddr_selftest_top.v` + `run_trace_ddr_selftest.tcl`，
就是厂商 `21_ddr3_test` 的移植版并加了以太网 :5001 状态出口。直接综合它、烧、跑
`scripts/ddr_selftest_status.py`（读 0xFF50-0xFF73），不用抄厂商工程也不用 ILA。

**2026-09-04 S1a 实测结果** ✅：BUILD_ID `0x6a9a3fc7`（trace_ddr_selftest.bit）烧板后：
- magic 0xD3、MIG calib OK、local calib OK、error sticky clean
- errc=0（10 s 观察全程零错）
- pass bytes 688→703 MB（~1.6 MB/s 写-读-比对，WRITE/READ FSM 交替）

**结论**：DDR3 硬件 + MIG IP + `ddr3_ctrl` 抽象层在这块板上零错。基础设施就绪，进 S1b。

### 3.4 S1b 落地清单

在 S1a 全绿的基础上，把 ramp 数据源、la_ddr_writer、la_ddr_ring_streamer、fpga_core_net
接起来。

**2026-09-04 S1b 进展（部分通、剩一个 race，已冻结待 r36 评审）** ⚠️：
- **99.61% 字节正确**（固定 0x42 源，实测 232M 字节中 232M-905K = 99.61% 是 0x42）
- **剩余 0.39% race**：错值分布均匀在 packet 内 1023 个位置，non-0x42 集中在 `0x01 / 0x80±3`
  等小值，模式看似 seq/rtx flag 或 partial-updated DDR word
- **根因假说**：writer 满速（100+ MB/s）持续 lapping streamer（drain 116 MB/s 网络限），
  ring 一直处于 overrun 状态；`avail_raw = (wr_ptr - rd_ptr) mod ring` 在 writer 越过
  rd_ptr **同一个 burst 写入进行中** 那一 clk，可能刚好 ≥ LENGTH*8 → streamer 判 burst_avail
  开始读 → 读到 **partial-written DDR word**（writer 还没写完这个 burst）
- **仿真复现挂**：`tb_ddr_ring_fixed.v`（0x42 + ramp 两种模式均 pass）用**行为化 MIG**
  1 clk 完成一 burst，**上板 DDR3 写 burst 需数十 clk** —— race window 在仿真里被压缩没了
- **doc 19 §7 P0e "concurrent R/W 零污染" 仿真结论没伪造**，只是没覆盖真实 DDR write burst
  耗时下的 race。tb_la_ddr_ring 也一样：drain 快到 write 完成前不会追上 wr_ptr
- **可能修复方向（未实施，待评审）**：
  - a) 让 streamer 保持一个 `LENGTH` 的**安全距离** —— `burst_avail = avail >= 2*LENGTH*8`
    永远不追到 writer 正在写的 burst
  - b) 让 writer 只在 `ddr3_wr_done` 后再更新 `wr_ptr_words`（现有代码就是这样，见
    la_ddr_writer.v:230），确认 avail_raw 只在 wr_done 到 rd_ptr 读同一 burst 之间出现
    race — 若是则问题在 rd_ptr 更新时机而非 wr_ptr
  - c) 在 tb 里模拟真实 DDR write burst 延迟（比如 app_wdf_rdy 每 burst 期间下拉 N clk）
    先复现 race
- **冻结**：动 `la_ddr_ring_streamer.v`（doc 19 上游 RTL）需要红方评审，本文档同期打提示词

**S1b 部分产物已可用**：
- `rtl/ddr_ring_selftest_top.v` 上板通过、99.61% 字节正确
- `sim/tb_ddr_ring_fixed.v` 离线复现器（0x42/ramp 模式），未来任何 S1b RTL 修改先跑这个
- `scripts/stream_grab.c` 零丢主机收流器（recv 线程 + 写盘线程解耦）
- `scripts/ddr_selftest_status.py` 状态读出工具（S1a、可扩展支持 0xD1 magic 的 S1b）

**S1b 完全通过的验收标准（99.61% → 100%）**：
- fixed 0x42 源：整段抓样非 0x42 计数 = 0
- ramp 源：`_rampcheck.py` 报 100% monotone-mod-256

三个改动（S1b 落地：已 commit）：

1. **RTL**：`trace_stream_top.v` 加 `USE_DDR_RING` generic + CSR bit `USE_DDR_RAMP`：
   ```verilog
   // clk200 域每周期 +1 的 ramp
   reg [7:0] ramp_cnt = 0;
   always @(posedge clk200) if (!sys_rst) ramp_cnt <= ramp_cnt + 8'd1;
   wire [7:0] wr_src_byte  = use_ddr_ramp ? ramp_cnt : cap_byte;
   wire       wr_src_valid = use_ddr_ramp ? 1'b1     : cap_valid;
   // 接 la_ddr_writer.cap_byte / cap_valid_in
   ```
2. **PC 工具**：`scripts/ddr_ring_selftest.py`，一条命令跑通:
   - 烧 `ddr_ring_selftest.bit`
   - 置 CSR `USE_DDR_RAMP=1`
   - 用 `stream_grab` 收 30-60 秒 payload
   - 调 `_rampcheck.py` 校验 monotone-mod-256 + 逐字节零破损
   - 读 `words_written / ring_overrun / wr_lost_bytes` CSR 打印总结
3. **综合**：`fpga_flow/run_trace_stream.tcl` 加 `USE_DDR_RING=1` 分支（generate 门控），
   把 S1a 生成的 MIG IP、`rtl/ddr3/*` 加入综合源列表

**估计工作量**：RTL 改动约 100-150 行 + tcl 修改 + PC 脚本 100 行 + 综合 2-3 次迭代
（DDR 时序收敛可能需要迭代）。上板后 30 分钟压测得到 S1b 验收结论。

---

## 4. RTL 变更清单（逐文件）

### 4.1 `rtl/trace_stream_top.v`

新增 generic：
```verilog
parameter USE_DDR_RING = 0,      // 0 = 旧 axis_async_fifo 直通；1 = DDR ring + streamer
parameter [28:0] RING_BASE  = 29'd0,
parameter [28:0] RING_WORDS = 29'h0800000,  // 16 MB（doc 19 §7）
parameter integer PKT_WORDS = 64            // 1 KB/包，和现有 STREAM_PAYLOAD 对齐
```

**generate 分支**（在现有 `g_stream` 内再分 `g_ring` / `g_no_ring`）：

`g_ring`（`USE_DDR_RING=1`）：
- 例化 `la_ddr_writer`：
  - `cap_clk = clk200`, `cap_byte = cap_byte`, `cap_valid_in = cap_valid`
  - `ui_clk`/`ui_rst` 引出到顶层 port，接 DDR3 controller
  - 输出 `wr_ptr_words / words_written / wr_lost_bytes` 送 streamer + CSR
- 例化 `la_ddr_ring_streamer`：
  - `wr_ptr_words / wr_words_committed` 来自 writer
  - `drain_credit = 1'b1`（S2 阶段不做限速，全速泄；后续如需 pacing 再加 clk 分频令牌）
  - `stream_tdata / tvalid / tready` 接原有 seq+payload 打包 FSM 的输入（**替换**当前
    `fifo_out_data / fifo_out_valid / fifo_out_ready`）
  - `stream_seq / stream_rtx` 也带到打包 FSM，让**每个包头**除了原有的 seq 字段外，
    再加 1 字节的 `rtx flag`（S3 阶段）
- **移除**原 `axis_async_fifo u_cdc`（streamer 内部有 161-bit 版本）
- **状态字节暴露**（CSR NB+ 若干字节）：`ring_overrun`, `wr_lost_bytes`, `words_drained`,
  `nack_busy`, `nack_fail` —— 供 `fpga_health.py` 读出诊断

`g_no_ring`（`USE_DDR_RING=0`，默认）：**完全保留现有代码**，不影响回退 bit。

### 4.2 `rtl/la_ddr_writer.v`（已存，无需改；确认接口匹配）

`la_ddr_writer` 已经用 `IN_BYTES=1`（cap_byte 单字节接口）、`cap_clk` 域接受，
CDC 到 `ui_clk` 域打包 128-bit 写 DDR，暴露 `wr_ptr_words + words_written`——
正是 streamer 期望的接口。**无需改动**。

⚠️ **RING_WORDS 语义对齐检查**：
- writer 的 `RING_WORDS` = 128-bit 字数（默认 29'd0100000 = 1M 字 = 16MB）
- streamer 的 `RING_WORDS` 是 app-address 单位（+8/128-bit-word）（默认 29'h0800000 = 8M app-addr = 1M 字 = 16MB）

**两者数值不同但物理容量一致**。top 集成时必须传入协调的一对参数：
```verilog
la_ddr_writer #(.RING_WORDS(29'h0100000)) u_wr(...)   // 1M words
la_ddr_ring_streamer #(.RING_WORDS(29'h0800000)) u_st(...)   // 8M app-addr = 1M words
```
写在 top 时用 localparam 派生，避免手工同步失误：
```verilog
localparam [28:0] RING_128B_WORDS = 29'h0100000;
localparam [28:0] RING_APPADDR    = RING_128B_WORDS << 3;
```

### 4.3 `rtl/fpga_core_net.v`

**新增 UDP RX :5002 分支**（现有 :5001 CSR 已有类似解析可参考）：

- 接收 :5002 的 UDP payload
- 匹配 `NK` 2 字节魔数（`nack_protocol.py` 已定义）
- 解出 `start_seq(4B BE)` + `count(2B BE)`
- 输出 pulse `nack_valid` + 稳定 `nack_start_seq / nack_count` 到 streamer（clk125 域）
- 忽略非 `NK` 魔数的包（保持 :5001 CSR 通路兼容）

**不动**现有 :5001 CSR 和 :5555 self-TX 路径。

### 4.4 `rtl/ddr3/*`

**无需改**。writer 和 streamer 都直连 `ddr3_ctrl` 的 wr/rd 端口，`ddr3_arbit`（写优先）
已经天然满足 doc 19 §7 "源头永不背压" 的硬约束。

### 4.5 综合流程 `fpga_flow/run_trace_stream.tcl`

新增可选 generic 直通：
```tcl
set use_ddr 0
if {[info exists ::env(USE_DDR_RING)]} { set use_ddr $::env(USE_DDR_RING) }
# ...
synth_design -top trace_stream_top ... -generic USE_DDR_RING=$use_ddr ...
```

`USE_DDR_RING=1` 时需要额外把 DDR3 MIG IP 加入综合源列表。**先确认 MIG IP 是否已在
`run_trace_ddr_selftest.tcl` 里就位并可直接抄**：

- 若 MIG IP 已存在（`ddr3_selftest.bit` 就是走这条 IP），复用其 `read_ip / xdc / bd`
- 若没有，先跑一次 `run_trace_ddr_selftest.tcl` 确认 IP 生成 + 时序清白，再合并到
  `run_trace_stream.tcl`

---

## 5. XDC / 时序策略

### 5.1 时钟域清单（doc 19 §7 已论过，这里落到 XDC）

| 时钟 | 频率 | 用途 |
|---|---|---|
| `trace_clk` | ~100-112 MHz | STM32 TPIU |
| `clk200` | 200 MHz | cap_byte 采集端 |
| `clk125` | 125 MHz | 网络/CSR |
| `ui_clk` | 100 MHz（MIG PHY 400 MHz / 4:1） | DDR3 用户接口 |
| `ddr3_ck` | 400 MHz | DDR3 时钟 |

**关键 CDC 路径**（`false_path` / `max_delay` 约束）：
- `cap_byte`(clk200) → writer 内部 async FIFO → DDR3 pack(ui_clk)：内部处理，
  writer v2 已论证零丢
- streamer wr_ptr / committed 追指针：**同 ui_clk 域**，doc 19 §7 P0e 明确点出不用 CDC
- streamer → clk125 async FIFO(161-bit)：streamer 内部处理
- NACK CTRL 域 → streamer ui_clk 域：streamer 内部 toggle-sync + 2FF

XDC 里对上述 async FIFO 的 gray-code 指针和 toggle-sync 已经由 `axis_async_fifo`
和 `la_ddr_ring_streamer` 内部管好；顶层**不需要额外的 timing exception**。

### 5.2 XC7A35T 资源预估

粗估 slice/BRAM/DSP 占用（doc 19 §11 论过但没落到数字）：
- 已有 STREAM=1 bit：~35-45% LUT 使用（历史综合日志可查）
- +MIG IP：约 +8% LUT + 数个 BRAM（vendor demo `21_ddr3_test` 有参考数字）
- +`la_ddr_writer`：async FIFO（几个 BRAM）+ pack logic（几百 LUT）
- +`la_ddr_ring_streamer`：161-bit async FIFO（1-2 个 BRAM）+ FSM（几百 LUT）

**估算总占用 < 70%**，A7-35T 应可容纳。上板 S1 后 `synth report` 出真实数字。
若 > 90% 或时序 WNS < 0.1ns，考虑：
- 降 `axis_async_fifo` depth
- 关掉某些诊断路径（`lost200` 计数器 CDC 可省）
- 把 streamer 的 161-bit FIFO 改成两个 128+33-bit BRAM

---

## 6. 端到端 P4 验证方法（S4 阶段）

### 6.1 注入丢包工具

用 Linux `tc netem` 在收流网卡上主动丢包：
```bash
sudo tc qdisc add dev enxc8a36266dcae ingress
sudo tc filter add dev enxc8a36266dcae parent ffff: matchall action \
    netem loss 1%
```

或者更精细：`--iface enxc8a36266dcae drop 100 in 10000`（每万包丢 100 个）。

### 6.2 判据（严格，doc 19 §14.5 精神）

1. **`stream_endurance` 报告 seq-gap 事件数 = tc netem 注入的丢包数**（相符率 > 99%，
   证明丢包检测正确）
2. **`nack_rx.py` 报告 hole = 0**（重传后字节完全补齐，是核心零丢证明）
3. **`nack_fail` 事件仅在超窗口丢包发生时触发**（>DDR ring capacity）
4. **`ring_overrun = 0`** 全程（说明 drain 追得上 writer）
5. **`wr_lost_bytes = 0`** 全程（说明 writer 侧无丢，源头没背压）
6. **压测时长 ≥ 1 小时**，字节级完整性（内容 diff 源头 = 收流）

### 6.3 覆盖注入模式

| pattern | 目的 |
|---|---|
| 均匀 1% 丢 | 基线：小概率抖动 |
| 均匀 20% 丢 | 高丢包：验证 NACK 压力下不雪崩 |
| burst 100ms 全丢 | 突发：验证 DDR ring 窗口够宽 |
| burst > DDR window 全丢 | 边界：验证 nack_fail 诚实上报 |
| duplicate + reorder | 乱序：验证 PC 侧 `ReliableReceiver` 去抖 |

---

## 7. 与 doc 19 已有内容的关系

**不重复**：
- 三层架构、seq↔DDR 绑定、nack_fail 边界、P0e 结论、离线仿真结果全在 doc 19 §1-§14
- 本文档只在 doc 19 §14.5 的一句话（"尚未上板：待综合... top 集成"）后接续

**补充**：
- 上板集成的五步分解 + 每步独立 bit + 每步独立判据（表 3）
- **S1 拆 S1a（复现厂商 DDR loop 压测证基础设施）+ S1b（加 writer/streamer/网络三层）**
  （§3.1-3.4）——doc 19 §7 P0e 的仿真结论上板实证 + 独立标定传输子系统零丢上限
- 具体 RTL 修改清单（§4）
- XDC/综合注意事项 + 资源预估（§5）
- 注入丢包工具 + 端到端判据（§6）

**doc 19 应做的呼应更新**（本文档合入后）：在 doc 19 §14.5 末尾加一行 `→ 见 doc 21`。

---

## 8. 风险 / 已知未决

| 风险 | 应对 |
|---|---|
| MIG IP 生成 + 时序收敛在 A7-35T 上不平凡 | S1 分离出来先跑 `ddr3_selftest`，走通再合并 |
| :5002 UDP RX 加进 fpga_core_net 破坏现有 :5001 CSR | 用魔数区分（`NK` vs CSR 的 `{addr,value}` 格式），互不干扰 |
| PC 端 `nack_rx.py` 生产模式尚未和 `stream_endurance` 合流 | S3 前把 `nack_rx.py` 的 live 前端并入 `stream_grab.c` 或 `stream_endurance.py` |
| 断电后 DDR 内容丢，重传窗口重置 | 已知边界：断电即重连，seq 从 0 起、ring 从头写；不再关心断电前的历史 |
| DDR 环形 wrap 时的边界字节 | tb_la_ddr_ring TEST A 覆盖了并发读写 wrap 前后连续 ramp，仿真已验证 |

---

## 9. 推进节奏建议

- 阶段 **S1a**（复现厂商 DDR loop 压测）：**最先做**，最快能给出"DDR 硬件+MIG IP+
  ddr3_ctrl 是否好"的独立结论。不改 orbtrace 代码，估 1-2 小时（含综合）
- 阶段 **S1b**（DDR ring + writer/streamer/网络，输入接内部 ramp 剥离 trace）：在 S1a 全绿
  的前提下，2-3 次综合迭代把 orbtrace top 改造完成。产物是**纯传输子系统的能力标定**
- 阶段 S2（真 trace 接进 writer 输入，其余不动）：验证 cap_byte→writer 的 CDC/时钟约束
- 阶段 S3（NACK RX + rtx）：加 UDP RX 层，端到端在硬件上跑
- 阶段 S4（P4 注入丢包验收）：最长一步，包含小时级压测

**为何 S1 拆两小步**：S1a 是"基础设施验证"（DDR 硬件 / MIG IP / XDC / ddr3_ctrl 层），
用厂商现成 demo 一步到位。S1b 是"新加的 writer/streamer/网络三层"。分开后任一层挂了都
定位精确，不用绕："S1a 挂 → 基础设施问题"；"S1a 好 S1b 挂 → 三层新代码之一有 bug"。

每阶段完成时更新 doc 19 §14.5 状态位（S1 ✅ / S2 ✅ / ...），本文档保持"计划"角色不动。
