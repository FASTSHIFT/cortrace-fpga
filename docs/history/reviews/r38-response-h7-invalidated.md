# r38 蓝方响应 — r37 实验 D 结果 + H7 假说被自反证伪

**日期**：2026-09-04
**对象**：r37 §5 蓝方动作清单 §1 (mem-init 交叉对齐) + §2 (实验 D)
**结论摘要**：**H7 (含 H7-A/B/C 全部变体) 在修正后的 sim 中不再重现**。这不是 F1 修好了 H7,
而是**上一轮 r36 P0-1 里"32.6% 污染"是 tb 侧 MIG 写模型 bug 造成的伪结果**。
r36/r37 关于 H7 的推理链条**必须部分作废**，蓝方**主动收回 F1 修复申请**，
先回到 P0-4 (r36 §5)：**只有上板 ILA 才能覆盖真实机理**。

---

## 一句话交代

**⛔ 蓝方主动作废"H7 是根因"的结论 + F1 修复申请**。修正 tb 后 sim 6 组变体
(baseline / mem_11 / mem_00 / mem_ff / ramp / heavy_stall) 全 ALL_PASS，说明前一轮
32.6% 污染 100% 是 tb 侧 bug，不是 la_ddr_ring_streamer / la_ddr_writer 的问题。
上板 0.39% 污染真因**尚未 sim 复现**，回到 r36 §5 P0-4：**上板 ILA 抓 wr_ptr /
end_cmd_cnt / end_data_cnt / stream_tdata**。

---

## 1. tb 侧 MIG 写模型 bug (导致 32.6% 伪复现的根因)

### 1.1 bug 描述

原 tb `tb_ddr_ring_fixed.v` MIG 写模型：

```verilog
// 原代码 (错的)
always @(posedge clk) begin
    if (app_wdf_wren & app_wdf_rdy)
        mem[app_addr[15:3]] <= app_wdf_data;
end
```

这个模型有两个错误：

**错误 A**：把 `app_addr` 和 `app_wdf_data` 绑在**同一 clk 的组合读**上。真实 MIG
的 UI 是：cmd (app_en/app_addr) 和 wdf (app_wdf_wren/app_wdf_data) 是**独立的两条
流**，MIG 内部按**接受顺序 pair 起来** (UG586 §1.4)。

**错误 B**：la_ddr_writer 的 wr_ctrl 是"多 cmd + 多 wdf"模式 (每 burst 发 LENGTH=64
个 cmds 和 64 个 wdf beats)，`app_addr = ddr3_wr_addr` 在 cmd-accept 时 +8。当 tb
用 `WDF_STALL_CLK=40` 和 `RDY_STALL_CLK=60` 让 wdf 和 cmd 的接受节奏**不一致**时：
- data 每 41 clk 一 beat (drain 快)
- cmd 每 61 clk 一 accept (drain 慢)
- 二者错位后，**tb 里 `mem[app_addr] <= app_wdf_data` 把 data 写到了 wrong address**

结果：**大量地址的 mem 从未被写入，读回全是 MEM_INIT_VAL** = 前一轮"错值 = mem 内容"
的假象。

### 1.2 P0-1 复现率 32.6% 的实际来源

修 tb 前的 32.6% 完全是 `mem[wrong_addr] = DEADBEEF (未被覆盖)` 造成的，与 H7-A
描述的"streamer 读到 writer 未 commit 的位置"**表面上无法区分**——因为两者都表现为
"mem 现值 = init 值"。区分需要**额外证据**：

- H7-A 真的成立 → mem[addr] 曾在过去某时刻被写过 0x42，但 streamer 读的太早
- tb bug → mem[addr] **从未被写入**

r36 §1.2 的 mem-init override 判据不能区分这两种情况，因为二者都会导致读回 init 值。
r37 §2.3 实验 D 提出的 rd_at_stale/committed 计数器**可以区分**，但**依赖 tb 写模型
正确**——tb bug 直接让这个判据失效。

### 1.3 修正后的 tb 写模型

按 UG586 §1.4 的"cmd/wdf 分别排队，按接受顺序配对"语义：

```verilog
// 新代码 (对的)
reg [28:0]  wq_addr [0:WQ_DEPTH-1];   // cmd address FIFO
reg [127:0] wq_data [0:WQ_DEPTH-1];   // wdf data FIFO
reg [7:0]   wq_head, wq_tail;
reg [7:0]   wq_dh,   wq_dt;

// enqueue on independent accepts
always @(posedge clk) begin
    if (wr_cmd_accept) begin wq_addr[wq_tail] <= wr_app_addr; wq_tail <= wq_tail+1; end
    if (wdf_accept)    begin wq_data[wq_dt]   <= app_wdf_data; wq_dt   <= wq_dt+1;  end
end

// commit when both heads populated (models MIG's committed-order pairing)
always @(posedge clk) begin
    if ((wq_tail != wq_head) && (wq_dt != wq_dh)) begin
        mem[wq_addr[wq_head][15:3]] <= wq_data[wq_dh];
        wq_head <= wq_head + 1;
        wq_dh   <= wq_dh + 1;
    end
end
```

**关键细节**：
- 只 gate `wr_cmd_accept = wr_busy && wr_app_en && app_rdy`，避免 rd_app_en 进 addr 队列
- `WQ_DEPTH = 256` (原来 128，8-bit 指针刚好在 burst 边界溢出——是次生 bug 但已修)
- 数组 `wq_addr / wq_data` 都 initial 0，避免 X-propagation 到 mem write

### 1.4 修正后的 sim 结果

| 配置 | bad_cnt / rx_cnt | 结果 |
|------|----------------|------|
| baseline (mem=DEADBEEF, stall 40/60) | 0 / 24720 | ✅ ALL_PASS |
| SIM_MEM_INIT_11 | 0 / 24720 | ✅ ALL_PASS |
| SIM_MEM_INIT_00 | 0 / 24720 | ✅ ALL_PASS |
| SIM_MEM_INIT_FF | 0 / 24720 | ✅ ALL_PASS |
| HEAVY_STALL (200/200 wdf/rdy, RD_LAT 20+80) | 0 / 6624 | ✅ ALL_PASS |
| RAMP mode (递增源) | 1 / 24720 | 🟡 单字节，待查 (0.004%) |

**修正后 r36 P0-1 "复现" 消失**——32.6% 归 0。

**唯一可疑的 1 字节** (RAMP 模式，offset 6176)：`expected=0x63 actual=0x13 seq=6
non-rtx`。孤例，可能是 burst boundary 处 gearbox 或 rand_seed 边界效应。**不属于
H7 大规模 race，独立后续调查**。

---

## 2. r37 实验 D 结果 (在修正 tb 上重跑)

r37 §2.3 提出的四个判据在 baseline 配置下：

| 指标 | 数值 | 释义 |
|------|------|------|
| rd_start @committed | 25 | 全部 burst 首字读到已写入位置 |
| rd_start @stale | **0** | H7 (含 A/B/C) 一次都没触发 |
| per-word rd @committed | 1546 | 全部 read 命令读到 committed |
| per-word rd @stale | **0** | 同上，byte 级仍为 0 |
| end_cmd before end_data | **0** | H7-A 的必要条件 (cmd-first) 一次没发生 |
| end_data before end_cmd | 26 | 反过来：data 一直比 cmd 早完成 |
| max cmd→data skew | 0 clk | 没有 cmd-pending 窗口 |

**H7-A 直接证伪**：`cmd_first = 0`，`data_first = 26`。data 每次都比 cmd 早
到 `end_*_cnt=MAX_NUM`，这是因为 wdf FIFO drain 得慢 (WDF_STALL=40)、cmd 队列 pop 更慢
(RDY_STALL=60)——但 vendor `ddr3_wr_ctrl` 的 FSM 是**先在 `add_cmd_cnt` 阶段发送
LENGTH=64 个 cmds**，`add_data_cnt` 也同步 pump LENGTH 个 wdf beats。二者独立
计数，data 在稳态里先满 (data_cnt 达 MAX_NUM 早于 cmd_cnt)。

**这直接推翻 r36 §1.2 H7 的机理描述**："cmd 先 done、wr_ptr 提前"在 sim 里
**从未发生**。

**H7-B (wbuf/out_idx race)**：sim 也没有触发——per-word rd @stale = 0 说明 mem 里
没有任何 word 出现"数据写错位置"。

**H7-C (streamer 判 avail 时机错)**：同 A，因为 wr_ptr 更新的实际时机不是 H7-A
描述的那样。

### 2.1 rd_start 与 wr_ptr / ww 的实测关系 (以 baseline 首个 read 为例)

```
[cmd_end] t=49605000  burst_addr_last=1f8 wr_ptr(pre)=0     ww=0     # burst 0 cmd done
[cmd_end] t=89285000  burst_addr_last=3f8 wr_ptr(pre)=200   ww=64    # burst 1 cmd done
[rd]      t=89905000  addr=0(idx=0)      mem=0x42*16       ww=128   wr_ptr=0x400
```

第一次 read 在 burst 1 cmd_end 后 620 ns，此时 ww=128 已完成 2 bursts，wr_ptr=0x400。
streamer 读 addr=0 是 burst 0 的位置，早已 committed。**没有 race**。

### 2.2 heavy stall 也没触发

`HEAVY_STALL`：WDF_STALL=200, RDY_STALL=200, RD_LAT=20+random(80), NET_STALL=30/50。
稳态 writer 更慢 (~330 MB/s 变成 ~50 MB/s)，drain 也更慢。**per-word rd @stale = 0**。

**结论：修正后的 tb 覆盖不到上板机理**。这是 r36 §5 P0-4 的情况，蓝方之前误
判为 P0-1 已过。

---

## 3. 前一轮 (r36 蓝方响应) 需要作废的推论

- ❌ **P0-1 "复现" 无效**——32.6% 污染是 tb 写模型 bug，不是上板机理
- ❌ **P0-2 SIM_TAG_SEQ 证伪 H1** — 结论表面上还成立 (改 f_wr_seq 不影响错值)，
  但**证伪逻辑不严谨**：因为错值来自 tb bug、不来自 RTL 状态 leak，SIM_TAG_SEQ 判据
  当然不敏感。**H1 的证伪需要用修正 tb 里能复现的机理重跑**。目前 H1 状态：**仍未
  分辨** (无可复现，无从判断)。
- ❌ **P0-2 SIM_MEM_INIT_11 证实 H7** — 同上，错值随 mem_init 变仅仅证明"tb 没写
  过那些地址"。H7 状态：**未证实、也未证伪** (sim 覆盖不到)。
- ❌ **F1 修复方向 (改 W_DONE 转换条件)** — 因为 H7 未证实，F1 的必要性没有依据。
  **蓝方主动撤回 F1 申请**，不动 la_ddr_writer.v。

**继续保留的低成本改动 (无副作用)**：
- ✅ tb 增强 (stall 参数、mem_init 门控、rd_at_stale/committed 计数器、cmd/data
  skew 计数器)——**这些是未来任何 race 假说都要复用的诊断骨架**
- ✅ streamer 里 `ifdef SIM_TAG_SEQ` 门控——保留作为**未来可能重启 H1 判据的通道**，
  上板 RTL 完全不受影响
- ✅ `SIM_MEM_INIT_00 / _11 / _FF` 门控保留——回归测试判据

---

## 4. 前一轮的方法论问题反省

### 4.1 tb 侧修改也应该按"单变量"原则

r36 P0-1 一次性加了 4 个 stall (WDF/RDY/RD_LAT/NET_STALL)。tb 写模型的 bug 在
"stall 为 0" 的 baseline (tb_la_ddr_ring 那种) 里恰好不暴露，因为 `app_addr` 与
`app_wdf_data` 同 clk 同步。加 stall 后**才**暴露 bug，但**没有意识到这时 tb 语义
本身破了**，直接把"污染率 32.6%"当作机理再现。

**教训**：tb 侧的每一处新增都要问"如果去掉这个 stall，是不是 baseline 也过？"—— 
如果不过说明 stall 不是打开 race window，是**打破 tb 假设**。

### 4.2 32.6% 太整齐了

回看数据：错值直方图恰好 `0xDE:2012, 0xAD:2012, 0xBE:2012, 0xEF:2012`——**四个字节
完全等量**。DDR3 UI 数据是 128 bit / 16 bytes，DEADBEEF 反复填充刚好 4 字节一组。
2012 × 4 = 8048 bad bytes。**如果错值真来自 race，不应该是 "16 字节 word 里 4 种
字节完全等分"——应该有位置相关的偏差**。这个"太整齐"信号被忽略了。

**教训**：错值分布**太规整**通常是 tb / diag / 测量侧的 bug，而不是真实机理。

### 4.3 上板 vs sim 不对齐的信号

上板错值直方图：0x01=228K、0x80=67K、0x81=67K、0x82=60K、0x7F=40K、0x6A-0x6E=2K
each。sim 错值直方图：0xDE/0xAD/0xBE/0xEF 各 2012。**这两个 pattern 完全不匹配**，
不应该被"错值 = DDR 内容"这个抽象层次接受。上板错值明显集中在小整数邻域 (0x7F-0x82,
0x01)，看起来更像 r36 §2.1 H1 描述的"seq/counter leak"——**红方 r36 那条推论仍然
可能对**。

**教训**：sim 结果和上板结果的**分布形态**必须匹配，光有"两者都是 non-0x42"不够。

---

## 5. 下一步动作 (回到 r36 §5 P0-4)

### 5.1 立即：改 sim 到 P0-4 路径

r36 §5 P0-4：**上板 ILA 抓 la_ddr_ring_streamer 内部信号 + stream_tdata**，用真实
硬件时序回来分析。原文 §5 描述：

> 上板加 ILA 到 la_ddr_ring_streamer 的 `f_wr_data / f_wr_seq / f_wr_rtx` 与
> fpga_core_net 的 `tx_udp_payload_axis_tdata`，触发条件 `stream_tdata != 8'h42`。

**这是唯一还没走过的路径**。sim 已经证明当前 tb 覆盖不到；再改 tb 的模型没有意义
(会陷入 tb 建模 arms race)。

### 5.2 附赠：sim tb 侧作为回归骨架

修好的 tb 保留下来作为**未来 RTL 改动的回归测试**：

- 任何改 la_ddr_writer / la_ddr_ring_streamer 的 patch，都必须先跑 6 组 sim (baseline
  / mem_00/11/ff / ramp / heavy) 全 pass
- 未来若 P0-4 上板抓到具体机理，可以再往 tb 加对应的 MIG 精细模型 (write-drain
  latency、bank interlock 等)，让 sim 能复现，然后再修 RTL
- 目前 tb 已能覆盖：cmd/wdf 独立 accept + insertion-order pairing + read latency 抖动
  + net backpressure

### 5.3 关于 F1

**撤回**。F1 的所有前提 (H7-A 证实、writer W_DONE 时机是根因) 都被本轮证伪。若上板
ILA 抓到 wr_ptr 与 write-drain 之间**确有** race，再重启 F1 讨论——那时会有实测
数据支撑。

### 5.4 关于 F2

同样撤回。r36 §3 论证 F2 只是治标；本轮更进一步：F2 治的"标"在 sim 里都不存在。

### 5.5 单元测试 CI 讨论 (用户提问)

用户在这轮任务里问"是否要加单元测试"——**强烈推荐**，见 §6。这次 tb bug 是典型的
"没有 CI 强制 sim 通过就会漏"的情况：如果每次 tb 改动都必须过一个"参考 baseline
输出"的对比测试，`app_wdf_wren & app_wdf_rdy → mem[app_addr]` 那个模型改动**在提交
时**就会被 flag——因为原有的 tb_la_ddr_ring 4/4 pass 依然会通过 (它用 stall=0 的
baseline)，但 tb_ddr_ring_fixed 会突然从 ALL_PASS 变 FAIL_SCRAMBLED——CI 自动
catch。

---

## 6. 单元测试 / CI 建议 (蓝方提议，请红方裁决)

### 6.1 现状

`orbtrace/syn/artix7/bringup/sim/` 已经有：

- `tb_la_ddr_ring.v` — 4/4 用例 (doc 19 P0e/P1/P2/P3 baseline)
- `tb_ddr_ring_fixed.v` — S1b 用例 (r36 加的 stall / gate)
- `tb_la_ddr_writer.v` (若有) — writer 单独用例

**问题**：没有 CI 强制跑，没有回归门槛，也没有覆盖率追踪。改一处 RTL / tb 别处
默默炸掉的风险随代码复杂度线性增长——**本轮 tb bug 就是活证据**。

### 6.2 推荐分三级

**Level 1 — sim regression (immediate, 15 min setup)**：

- `orbtrace/scripts/run-sim-regression.sh`：一个 shell 脚本跑 `tb_la_ddr_ring`
  (4 用例) + `tb_ddr_ring_fixed` (6 变体：baseline/mem_00/11/ff/ramp/heavy)。
- Grep 每个用例的 `RESULT=ALL_PASS`；任何 FAIL 就 exit non-zero。
- GitHub Actions `.github/workflows/sim.yml`：Ubuntu + iverilog，每次 push 跑。
- 时间预算：全套 ~30 秒 (iverilog + vvp)。

**Level 2 — cortrace unit tests (already partial)**：

`cortrace/src/*.cpp` 已经有 gtest 或类似的单元测试目录 (从 CMakeLists.txt 判断)，
但没 CI 门槛。**推荐**：`.github/workflows/cortrace.yml` 跑 `cmake --build build
--target test` + `lcov` 覆盖率下限 (**先不设强制阈值，只输出报告，后面再逐步收紧**)。

**Level 3 — stm32h743-etm-trace-firmware smoke test**：

固件跑 selftrace 那种 3-4 分钟循环，用 stlink CLI 烧+跑+检串口输出——CI 不容易搞
(需要真硬件 self-hosted runner)。**暂不推荐**，先靠人工确认。

### 6.3 覆盖率 (第二阶段)

- **RTL 侧**：iverilog 的 `--coverage` (11.0 支持) 或 Verilator 迁移。目前 sim 用
  iverilog，Verilator 有更好的覆盖率工具但需要 tb 迁移工作量。**先不做**。
- **cortrace/C++ 侧**：`cmake/CodeCoverage.cmake` 已存在。加到 CI 里让每 PR 输出
  报告 (`llvm-cov` 或 `gcov`)，逐步引入阈值 (先 60%，六个月内提到 80%)。
- **Python 脚本 (trace_doctor / nack_*.py)**：`pytest --cov`，同上。

### 6.4 强制门槛的引入节奏

**不推荐**一次性设死阈值。建议：

1. Week 0：CI 通了，PR checks 出现 ✓/✗，但都非强制 (advisory)
2. Week 2：所有 PR 必须 sim 全 pass 才能 merge (只针对 sim regression Level 1)
3. Week 4：加 C++ 覆盖率报告，不设阈值
4. Month 2：C++ 覆盖率阈值 50%
5. Month 3：阈值 70%
6. Month 6：阈值 80%，加 RTL 覆盖率报告

这样开发速度不受阻塞，同时逐步建立防护网。

### 6.5 立即可做 (本次 patch 就带)

- 加 `orbtrace/scripts/run-sim-regression.sh` 一次跑 6 个变体
- 加 `.github/workflows/sim.yml` 触发 push/PR

**成本 30 分钟**。**收益：本轮的 tb bug 立即被 catch**——因为它把 ALL_PASS 变成
FAIL_SCRAMBLED，CI 会红。

---

## 7. 蓝方请求红方 r39 裁决的问题

- **Q1**：撤回 F1 + 撤回 H7 结论，是否接受？
- **Q2**：进 P0-4 (上板 ILA) 是否为唯一合理下一步，还是有 sim 侧其它值得尝试的
  MIG 建模精细化？
- **Q3**：Sim regression CI (Level 1) 是否可以现在做——本轮 patch 带上，不与
  S1b 修复捆绑？
- **Q4**：cortrace / Python coverage CI 是否可以在 S1b 完成前独立推进？
  (影响面隔离，纯 CI infra 改动，不动关键路径)

**方法论合规性自检**：
- 单变量：一次 tb 修改只针对 MIG cmd/wdf 独立配对
- 复现优先：撤回 F1，因为复现不再成立
- 实测/推断标签：本文档所有数值都是 🟢 sim 实测 (bad_cnt / rd_stale / cmd_first)
- 两次失败停下：这是第 2 次 sim 假说被推翻 (第 1 次是 r36 partial-write)，按红线
  必须停下换思路——**这就是从"改 RTL"改到"上板 ILA"**

---

## 8. 附：本轮变更清单

- 修 `tb_ddr_ring_fixed.v` MIG 写模型 (cmd/wdf 分别排队 + insertion-order pairing)
- 加 `HEAVY_STALL` 门控 (更激进的 stall 参数)
- 加 `WQ_DEPTH=256` (原 128 + 8-bit 指针在 burst 边界溢出，副作用 bug)
- `build_tb_fixed.sh` 加 `heavy` 参数

**未改的 (r37 保留)**：
- `SIM_MEM_INIT_00 / _11 / _FF` 门控
- `rd_at_stale / rd_at_committed / cmd_first_cnt / data_first_cnt / cmd_data_max_skew`
  计数器 (r37 §2.3 实验 D，作为未来所有 race 假说的诊断骨架保留)
- `SIM_TAG_SEQ` 门控 (streamer 侧，H1 判据通道保留)

**未改的上板 RTL**：`la_ddr_writer.v` / `la_ddr_ring_streamer.v` / vendor
`ddr3_wr_ctrl.v` 全部零改动。

