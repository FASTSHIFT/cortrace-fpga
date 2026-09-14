# r36 — S1b 剩余 0.39% 字节污染根因假说 红方评审

**日期**：2026-09-04
**对象**：`stage4-datapath/21-nack-onboard-integration-plan.md` §3.4 "S1b 进展 + 根因假说 + 拟修方向 a/b/c"
**复核 RTL**：`la_ddr_writer.v`、`la_ddr_ring_streamer.v`、`ddr_ring_selftest_top.v`、
`ddr3_wr_ctrl.v` / `ddr3_rd_ctrl.v` / `ddr3_arbit.v`（vendor 21_ddr3_test 同源）、
`fpga_core_net.v`（g_stream 分支 self-TX FSM）
**复核仿真**：`tb_la_ddr_ring.v`（4/4 ALL_PASS，行为化 MIG 无背压）、`tb_ddr_ring_fixed.v`
（0x42/ramp 双模式 ALL_PASS，同上）
**实测事实**：99.6117% = 0x42，0.3883% ≈ 905 K 字节非 0x42；错值集中于
`0x01(228K) / 0x80(67K) / 0x81(67K) / 0x82(60K) / 0x7F(40K) / 0x6A-0x6E(≈2K 每个)`；
`ring_overrun=1` sticky、`wr_lost_bytes=0`、`words_written` 单调；packet 内 1023 个字节位
错误几乎均匀，word 内 16 位置也几乎均匀，word-in-packet 有 25% 起伏但不集中。
**立场**：严格证伪。蓝方 4 次综合迭代 + 一个未复现出 race 的 tb + 一条"上板 DDR burst 耗时被
仿真压缩"的行为学论述 = 尚未构成动 RTL 的证据阈值。方法论第 3 条（一个方案试 2 次不成停下换
思路）+ 第 4 条（复现优先于修复）明确指向"必须先复现再动 RTL"。

---

## 一句话裁决

**⛔ 不允许进入 `la_ddr_ring_streamer` 修改阶段**。蓝方"writer 越 rd_ptr 时 partial-word read"
假说**不是唯一能解释观察的机制**，且从错值分布看**不是最可能的机制**（0x80/0x81/0x82/0x01
高度集中不符合 partial-write 应给出的 0xFF/0x00/stale-mixed pattern，反而符合"打包/位对齐/
FIFO 状态 leak"）。`ring_overrun=1` **是 sticky 一位**，不等于"持续套圈"（速率账算下来 writer
≈ drain 甚至略低于 drain 时也会有瞬时 sticky 触发）。**先做 P0-1：在 tb 里复现 ≥0.1% 污染**，
才准许动 streamer 一行代码。拟修方向 a)（`burst_avail = avail >= 2*LENGTH*8`）就算不误伤，
在真的稳态 writer > drain 时也只延迟 overrun 不治本；在 writer < drain 时是纯浪费。

---

## 1. 验证假说的完整性——蓝方假说不是唯一，甚至可能不是最可能

蓝方假说：writer 满速 lapping streamer → `avail_raw = (wr_ptr − rd_ptr) mod RING` 在
writer 越 rd_ptr 那一 clk 恰好 ≥ `LENGTH*8` → streamer 判 burst_avail 开始读该 burst 地址
→ 读到 partial-written DDR word。

### 1.1 该假说的**结构性硬伤**（在动 RTL 前必须先回答）

**硬伤 A：wr_ptr 只在 W_DONE 后更新，不在 burst 期间。**
读 `la_ddr_writer.v:230` 附近的 W_DONE 分支：
```verilog
W_DONE: begin
    words_written <= words_written + LENGTH;
    wr_ptr_words  <= wr_ptr_words + (LENGTH<<3);   // 或 wrap
    burst_done    <= 1'b1;
    wst           <= W_IDLE;
end
```
`wr_ptr_words` 只在 `ddr3_wr_done` 为真进入 W_DONE 的那一 clk 递增。`ddr3_wr_done` 来自
`ddr3_wr_ctrl` 的 `end_cmd_cnt`（`app_en & app_rdy & cmd_cnt==MAX_NUM` 之后一 clk）。
**即：wr_ptr 一次跳一个完整 burst（LENGTH*8=512 app-addr），streamer 看到的 avail_raw 不会
在 burst 中间跳到"半个 burst"或"3/4 个 burst"的中间值**。因此蓝方描述的"burst 写入进行中那
一 clk 恰好 ≥ LENGTH*8"**在这份 RTL 里不可能出现**——除非 wr_ptr 的更新时机被 shift 提前
（现在的代码没有）。蓝方在自己拟修 b) 里也观察到"现有代码就是这样"，那这个观察**否证了 a)
的假说前提**，蓝方没意识到这点。

**硬伤 B：MIG UI 保证 write→read ordering，不返回 partial data。**
UG586 (7-series MIG DDR3) §1.4 明确：*"The command port arbitrates requests from the
UI, ordering reads and writes to prevent RAW hazards to the same address."* 即使我们通过
`ddr3_arbit` 做了一层"写优先"的 arbitration，wr_ctrl 把 `app_en`/`app_wdf_wren` 交给 MIG
之后，MIG 内部保证 read 到已提交 write 地址的数据是完整 128-bit。要否证这点必须给出
MIG 违反其手册的实测证据（例：ILA 抓 `app_wdf_wren`/`app_addr` 与 `app_rd_data` 的 shell
逐字节比对），蓝方目前**只给了"上板 DDR write burst 需数十 clk"**这个说法作为佐证——那
是 MIG 内部实现细节，不代表用户接口不保序。

**硬伤 C：错值 pattern 与 partial-write 的预期不符。**
partial-write 或 lapping-overwrite 情况下，读到的字节应该是：(1) DDR3 上电默认位（0xFF 或
0x00，取决于 refresh 状态）；(2) 上一个 lap 写入的字节（同为 0x42，因为源恒定）；(3) 若真的
是"写了半个 word"则应该是 0x42 与 stale 的混合，stale 值不会集中在 128/129/130 三个相邻整
数。**观察到的 0x80/0x81/0x82 是三个连续整数占了 194 K 字节**，配上 0x7F(=0x80−1) 40 K
和 0x01 228 K，这**极像某个 8 位计数器/flag 的具体值 leak 到 payload**，而不是 DRAM 未定义
态。

**硬伤 D：ring_overrun=1 是 sticky，不证明持续 lapping。**
`la_ddr_ring_streamer.v`：
```verilog
if (overrun_now) ring_overrun <= 1'b1;
```
没有 clear。一次瞬时 backlog 超阈值（比如 calib 完成后的短暂 spike、或某次
`drain_credit`/`fifo_has_room` 抖动）就永远拉高。上板 sticky=1 只证明"曾至少发生一次"，
不证明"稳态套圈"。**这条推论蓝方拿反了**——用一位 sticky 反推持续状态是方法论第 1 条
（把假设当事实）的典型错误。

**硬伤 E：速率账算下来 writer 未必 > drain。**
top 里 cap_clk = ui_clk（同一时钟），src ramp 每 clk +1 = 1 byte/ui_clk。ui_clk 若为
100 MHz（Xilinx MIG DDR3-800 标准 4:1 PHY），writer 上限 100 MB/s。drain 上限=网络 116
MB/s（AGENT.md §2 AX88179 静默丢帧天花板）。**100 < 116**，稳态 writer 追不上 drain，
应无持续 overrun；仅在启动初期或 drain 抖动时短暂 sticky。蓝方"writer 侧速率显著高
(overrun 恒真)"是从 sticky bit 反推的**推断**，不是速率实测。请蓝方先给出一段
**writer_bytes_per_second 与 drain_bytes_per_second 各自的数值**（words_written/时间
vs stream_grab 报告的 MB/s），实测数字若 writer < drain，A 假说的基础前提就塌了。

### 1.2 至少 4 个替代根因（可测量判据排除或保留）

**替代根因 H1：streamer 内部 161-bit async FIFO 位对齐/打包错**
- 机理：streamer 把 `{f_wr_rtx[1], f_wr_seq[32], f_wr_data[128]}` = 161 bit 送入
  `axis_async_fifo(DATA_WIDTH=161)`，读侧再用 `m_fifo_out[127:0]` / `[159:128]` / `[160]`
  拆开。161 bit 非 8/32/64/128 对齐，综合工具在 BRAM/distributed RAM 上的 pack 可能有
  "奇数 bit 位置读回被邻位污染"的问题（尤其若合成时 BRAM 用了 36 Kbit 模式外加 parity
  bit 复用）。
- 支持证据：0x80/0x81/0x82 = 1000_00**xx** 的低 2 bit 变化——**极像 seq 最低字节被
  邻位（rtx 或 seq[8]）扰动**。0x01 = 0000_0001——极像 rtx 位（1 bit）leak 到 data byte
  的 LSB。0x7F = 0x80−1，也可能是 seq 低字节的 borrow。
- 判据（可执行，无需上板）：把 sim `tb_ddr_ring_fixed.v` 里 streamer 的
  `f_wr_seq` 改成非 0（例如 `f_wr_seq <= 32'h8081_8283`），重跑，观察 stream_tdata 出错值
  是否变成 0x80/0x81/0x82/0x83。若能复现，**这就是根因**，与 partial-write 无关。
- **相对权重：H1 > 蓝方假说**，因为它**在 tb 里就能复现**（假设我猜的机理成立），
  不需要"真实 DDR burst 耗时"这类仿真无法覆盖的物理时序。

**替代根因 H2：top packetiser 的 `stream_tready` gating 与 gearbox refill 边界 race**
- 机理：`ddr_ring_selftest_top.v` 里
  ```verilog
  wire pkt_tvalid    = pkt_active & (in_header | stream_tvalid);
  assign stream_tready = pkt_active & ~in_header & pkt_tready & pkt_tvalid;
  ```
  header 4 字节输出期间 `stream_tready=0`，gearbox 保持第一个 word 的 byte0。进入 payload
  第 0 字节后 `stream_tready` 才拉高。**gearbox refill 只在 `gb_empty=1` 发生**
  （`m_ready = gb_empty`）——如果 refill 那一 clk 恰好 pkt_tready 也拉高，会不会出现
  "读到的 gb_word 是旧 word 或新旧混合"？逐 bit 看代码目前没这问题，**但 stream_seq 是
  gb_seq 的组合输出，而 gb_seq 在 refill 时更新到新 word 的 seq**——latched_seq 在 pkt
  起始 latch，随后 header 4 字节输出，**如果这 4 字节输出期间 gearbox 发生了 refill**
  （不太可能，因为 tready=0，但若综合后有 combinational glitch），latched_seq 会不会
  latch 到错的 seq？这也需要 tb 补一个"header 期间 gearbox 恰好完成一个 word"的场景。
- 判据：在 sim 里插入 assertion：`always @(posedge clk125) if (in_header && stream_tready)
  $error("tready during header");`；用 `$fdisplay` 打印 latched_seq 与后续 stream_seq
  是否始终相等。
- **相对权重：H2 中低**，代码逻辑上看起来对，但需要 sim 验证 sequence 覆盖。

**替代根因 H3：`fpga_core_net.v` self-TX FSM 的 `send_pad` 塞 0x00 造成边界字节污染**
- 机理：g_stream 分支 line 549 附近：
  ```verilog
  wire send_pad = (st == ST_SEND) && !stream_tvalid && send_stuck;   // ~8ms underrun
  ...
  tx_udp_payload_axis_tdata = self_busy ? (send_pad ? 8'h00 : stream_tdata) : ...
  ```
  如果 stream_tvalid 掉 8ms（例如 gearbox 空 8ms），会填 0x00 到 payload。
- 判据（快）：错值分布里 **0x00 计数 = 0**（AGENT.md 提到的 histogram 不含 0x00）→
  **可直接排除**。若上板后加一路 CSR 计数 `send_pad_pulses`，读出应为 0。
- **相对权重：H3 已可排除**，但列出以完整覆盖 self-TX 路径。

**替代根因 H4：CSR bit `src_fixed_125` CDC 切换瞬态或 clk200 域 stray 字节**
- 机理：CSR 0x0B 通过 clk125 域寄存器 `src_fixed_125` → 两级同步到 ui_clk 生成
  `src_fixed`。切换瞬间的 2-3 clk 里，`ramp` 还没停但 `src_byte` 已经切到 0x42（或反之）。
  这只影响切换那一瞬间。
- 判据：AGENT 说抓样 = "30 秒 + 60 秒后 grab 233 MB"，切换在头几 ms 结束，之后
  99.99% 时间 `src_fixed=1` 稳定 → **切换瞬态最多影响头几百字节，无法占 0.39% = 905 K
  字节**。**可排除**。
- **相对权重：H4 已排除**。

**替代根因 H5：`ddr3_wr_data = wbuf[out_idx]` 组合读毛刺 + 综合优化差异**
- 机理：la_ddr_writer 的 `wbuf` 是 distributed/BRAM 数组，`ddr3_wr_data` 直连
  `wbuf[out_idx]`。out_idx 在 ddr3_wr_data_req 时递增，MIG 在同 clk latch wdf_data。
  如果综合把 wbuf 推成 BRAM 且未 register out_idx 到 addr 输入端，可能有 1 clk skew，
  导致每 burst 首/尾字节错位。
- 判据（快）：Vivado synthesis report 里搜 `wbuf` 的 map（BRAM vs LUTRAM），检查
  `ddr3_wr_data` timing path 有无 ILA 采样点。或跑 post-synth simulation 复现。
- **相对权重：H5 中低**，但**上板 vs 仿真差异大部分从这里来**——蓝方 tb 用行为化 MIG
  1 clk 完成 burst，恰好把这类 1-clk skew 掩盖了。

**替代根因 H6：`words_drained / PKT_WORDS` 综合器实现问题**
- 机理：streamer `f_wr_seq <= words_drained / PKT_WORDS`。PKT_WORDS=64=2^6 是编译时常数，
  正确综合应该是 `words_drained[31:6]`。若综合器某种情况下没优化（例：Vivado 2021.1 在
  某些 pragma 下退化为 iterative divider），会产生多 clk 组合链或错位。
- 判据（快）：post-synth netlist 搜 `divider` 或 `divmod` 单元；或改写为显式
  `f_wr_seq <= words_drained >> $clog2(PKT_WORDS)` 重综合一次比对。
- **相对权重：H6 中低**。

### 1.3 综合评估

- **蓝方假说唯一性 = ❌**：至少 H1/H2/H5/H6 是与 partial-write 完全独立的机理，且 H1
  与观察到的错值 pattern 匹配度**明显高于**蓝方假说。
- **必要条件缺失**：蓝方需要额外证明 (a) wr_ptr 在 burst 期间会跳步（RTL 上不成立），
  (b) MIG 返回 partial data（违反手册），(c) 稳态 writer > drain（速率账不支持）——
  三条中至少两条目前是**推断**，未实测。

---

## 2. 检验证据——错值 pattern 的字节级还原路径

### 2.1 错值分布 vs 各假说的预期匹配度

| 错值 | 计数 | Partial-write 假说预期 | H1 (FIFO 打包/位对齐) 预期 | H2 (packetiser race) 预期 | H5 (wbuf 综合 skew) 预期 |
|------|------|----------------------|--------------------------|-------------------------|------------------------|
| 0x01 (1) | 228 K | ❌ 不解释 | ✅ 1 bit flag (rtx) leak | ⚠️ 部分解释 | ⚠️ 若首/尾字节固定错 |
| 0x80 (128) | 67 K | ❌ | ✅ seq/counter MSB=1 | ❌ | ⚠️ 可能 |
| 0x81 (129) | 67 K | ❌ | ✅ 邻位 seq+1 | ❌ | ⚠️ 可能 |
| 0x82 (130) | 60 K | ❌ | ✅ 邻位 seq+2 | ❌ | ⚠️ 可能 |
| 0x7F (127) | 40 K | ❌ | ✅ seq−1 borrow | ❌ | ⚠️ 可能 |
| 0x6A-0x6E | 2 K each | ❌ | ⚠️ seq 高字节 leak | ❌ | ❌ |

- **partial-write 假说的致命弱点**：预期错值应该是 DRAM 未定义态（0xFF 集中，或 stale
  0x42 显示为 "正确"看不到错），不应该是**极窄的整数邻域**。
- **H1 假说的强支持**：0x80/0x81/0x82/0x7F 是 128 ± 2 的邻域，**符合 seq 或 counter
  低字节被邻位（rtx/seq[8]）污染的 1-bit 抖动**。0x01 = 单独的 rtx flag bit 落到 payload
  byte 位置。0x6A-0x6E 是 seq 或 words_drained 某个高字节的当前值。
- **必要实验**：把 streamer 里 `f_wr_seq` 改成 tag 值（例如常量 32'hA5A5_5A5A），若错值
  变成 0xA5/0x5A 集中，**H1 立刻定成主根因**。这个实验**在 tb 里就能跑**，不需要综合。

### 2.2 packet 内位置分布的解读

- **1024 字节位置都有错、几乎均匀**：排除"某个固定 offset 的常数注入"（那样应该只有几个
  特定 offset 有错）。
- **16 word 内位置几乎均匀（56K-57K 每位置）**：**支持 H1**（如果 seq 每 16 byte 一次
  leak，且相位随 seq 值变化，效果就是均匀分布）。partial-write 会导致 128-bit 边界处
  错值集中，与观察**不一致**。
- **word-in-packet 头 9/尾 21 每个 ~16 K vs 中间 34 每个 ~12 K（25% 起伏）**：这**很有
  意思**——头/尾 word 错误率高，中间平缓。**partial-write 应给出"burst 开始或结束的
  word 错"**（LENGTH=64，PKT_WORDS=64，即一个 packet 恰好一个 DDR burst）→ 若是
  burst 边界效应，应该头几个 word 或尾几个 word **显著**高于中间。25% 起伏边界看起来
  更像 **网络 UDP 分片/gearbox 与 seq boundary 的耦合**——每包 seq 变化的位置。

综上，**字节级还原路径高度指向 H1（FIFO/位对齐/打包）**，不是蓝方假说。

---

## 3. 拟修方向 a) 的风险量化

蓝方拟修 a)：`burst_avail = avail >= 2*LENGTH*8`（保持 1 个 burst 安全距离）。

### 3.1 稳态影响量化

假设稳态：writer 速率 R_w（bytes/s），drain 速率 R_d（bytes/s），ring 容量 C（bytes），
burst 大小 B = LENGTH*16 = 1024 bytes。

- 当前门槛：drain 只要 `avail >= B` 就开始读，稳态平均 backlog ≈ B/2（drain 追平 writer）。
- 拟修后：drain 要求 `avail >= 2B` 才开始读，稳态平均 backlog ≈ 3B/2（多缓一个 burst）。
- **稳态 drain 速率不变**（都由下游网络决定），只是平均 backlog +B。

### 3.2 R_w vs R_d 的三种情况

- **情况 α（R_w < R_d，我算的最可能情况：100 MB/s < 116 MB/s）**：
  drain 追得上，backlog 稳定 = B/2 → 拟修后 = 3B/2，都远小于 C = 16 MB，**不会 overrun**。
  但 **drain 每包多等一个 burst 时间 ≈ 20 µs（100 MHz ui_clk）** → 网络包间隔加 20 µs，
  对 116 MB/s 稳态压测**几乎无影响**（1 KB / 20 µs = 50 MB/s 额外开销预算 → 实际
  drain 稳态下会重叠 pipeline，实测影响 < 5%）。
  **结论：情况 α 下拟修 a) 无害无益**——真正问题**不在 avail 阈值**。

- **情况 β（R_w ≈ R_d ± 5%，稳态临界）**：backlog 会大幅波动，overrun sticky 可能
  多次触发。拟修 a) 让 backlog 平均 +B → **反而更容易触到 C 上限**。
  **结论：情况 β 下拟修 a) 是负优化**。

- **情况 γ（R_w > R_d，稳态套圈）**：
  当前 backlog 单调涨到 C 后 overrun 永久持续。拟修 a) 让 backlog 涨到 C 的时间提前
  B/R_w 秒（B=1KB, R_w=100 MB/s → 10 µs 提前）。**根本治不了 overrun**——只推迟了
  10 µs。要治稳态倒挂**只能降 writer 或加宽 drain**，不是加安全距离。
  **结论：情况 γ 下拟修 a) 治标不治本**。

### 3.3 论断

**拟修 a) 无论何种速率情况都不会解决观察到的 0.39% 污染**（因为它调的是 avail 阈值，
而 partial-write 假说本身即使成立也需要 avail 精确到 burst 中间才发生，加 B 后仍然会
在 `avail = 2B` 那一 clk 触发——只是移动触发点不消除触发条件）。

**拟修方向应换成**：验证 H1（FIFO 打包）是不是主根因，因为它是**唯一在 sim 里可复现**
的假说；partial-write 假说本身也需要 sim 环境（更精细的 MIG 行为模型）才能证实或证伪。

---

## 4. 复现优先——tb 修改方案

**蓝方 tb 全 pass 是因为行为化 MIG 太干净**——`app_rdy=1`, `app_wdf_rdy=1` 恒真，
read latency=2 clk（`tb_ddr_ring_fixed.v`）或 6 clk（`tb_la_ddr_ring.v`），没有 arbiter
grant 延迟、没有 DDR3 refresh stall、没有 write→read hazard interlock。

### 4.1 建议的 tb 增强（`tb_ddr_ring_fixed.v` 就地改，不要新写）

**优先级 P0（先跑，若能复现 ≥0.1% 污染就够了）**：

1. **拉低 `app_wdf_rdy` 每 burst 期间 N clk**——模拟真实 DDR3 write burst 忙碌：
   ```verilog
   reg [7:0] wdf_stall_cnt = 0;
   reg       wdf_rdy_r    = 1'b1;
   always @(posedge clk) begin
       if (app_wdf_wren && wdf_rdy_r) begin
           wdf_stall_cnt <= 8'd40;    // 40 clk stall per data beat
           wdf_rdy_r <= 1'b0;
       end else if (wdf_stall_cnt != 0) wdf_stall_cnt <= wdf_stall_cnt - 1'b1;
       else wdf_rdy_r <= 1'b1;
   end
   assign app_wdf_rdy = wdf_rdy_r;
   ```
2. **拉低 `app_rdy` 每 burst 完成后 N clk**——模拟 MIG bank activate/precharge：
   ```verilog
   reg [7:0] rdy_stall = 0;
   always @(posedge clk) begin
       if (app_en && (app_cmd == 3'b000)) rdy_stall <= 8'd60;
       else if (rdy_stall) rdy_stall <= rdy_stall - 1'b1;
   end
   assign app_rdy = (rdy_stall == 0);
   ```
3. **让 read latency 抖动**：把当前的 `RD_LAT=6` 改成 `6 + $random%20`。
4. **writer/drain 速率比按上板 ~1:0.9 配**：改 stream_tready 每 N clk 拉低一次
   （模拟网络反压 116 MB/s < writer 100 MB/s 的反向情况——虽然我算的是 writer < drain，
   蓝方观察是 overrun 恒真，两种都试）。

**优先级 P1（若 P0 复现，加下面几个精化）**：

5. **打印 stream_tdata 与源 ramp 逐字节 diff**——现在的 tb 只报 total bad_cnt，改成
   打印每个 bad 字节的 (packet_seq, offset_in_packet, expected, actual)，10 分钟就能
   看出 pattern。
6. **注入 H1 判据**：临时把 la_ddr_ring_streamer 里
   `f_wr_seq <= words_drained / PKT_WORDS` 改成 `f_wr_seq <= 32'hA5A5_5A5A`
   （**注意：改的是 sim override，用 `ifdef SIM_TAG_SEQ` 门控，不动上板 RTL**），
   看错值是否变成 0xA5/0x5A 主导。
7. **注入 H5 判据**：在 la_ddr_writer 的 wbuf 读侧临时改 `ddr3_wr_data = wbuf[out_idx]`
   为 `wbuf[out_idx] & 8'hFE`（清 LSB），看错值 0x01 是否消失。

### 4.2 关键约束

- **禁止改 la_ddr_ring_streamer.v / la_ddr_writer.v 上板 RTL** 直到 tb 复现 ≥0.1% 污染。
- **tb 里做 sim-only override 是允许的**（用 `ifdef` 门控）——那是诊断实验，不是修复。
- 每加一条 stall 记一个"实测/推断"标签：`app_wdf_rdy` stall 40 clk 是**推断**（真实
  值需要抓 ILA 拿 MIG 内部信号），但目的是**制造 race window**，不是精确匹配真机。

---

## 5. P0 复现步骤清单（可执行）

**红方先跑一次修改后的 tb，确认能复现 ≥0.1% 污染**，才准许蓝方进入 RTL 修改。

### P0-1 tb 增强 + 复现

```bash
cd /media/vifextech/huge/hwtrace/orbtrace/syn/artix7/bringup/sim

# 步骤 1: 备份现有 tb（成熟方案不动）
cp tb_ddr_ring_fixed.v tb_ddr_ring_fixed.v.baseline

# 步骤 2: 加 P0 §4.1 §1-4 的 4 个 stall 到 tb（就地改，不新建文件）
#   - app_wdf_rdy stall 40 clk / write beat
#   - app_rdy stall 60 clk / burst
#   - RD_LAT 抖动 6+random(20)
#   - stream_tready 每 100 clk 拉低 10 clk

# 步骤 3: 加逐字节 diff 打印
#   - 在 always @(posedge clk125) 里，if (stream_tdata !== 8'h42)
#       $fdisplay(fd, "%0d,%02x", $time, stream_tdata);

# 步骤 4: 跑
./run_tb_ring.sh tb_ddr_ring_fixed          # 用现有 runner；若无，iverilog + vvp
# 或
iverilog -o tb_ddr_ring_fixed.vvp -I ../rtl \
    tb_ddr_ring_fixed.v ../rtl/la_ddr_writer.v ../rtl/la_ddr_ring_streamer.v \
    ../../../04_source_code/A7_lite_demo_35T_new/A7_lite_demo_35T/21_ddr3_test/\
ddr3_test.srcs/sources_1/new/ddr3_wr_ctrl.v \
    <ddr3_rd_ctrl.v> <ddr3_arbit.v> <axis_async_fifo.v>
vvp tb_ddr_ring_fixed.vvp | tee run.log

# 判据：
#   - bad_cnt / total_rx >= 0.001 (0.1%)   -> P0-1 ✅ 复现成功，进 P0-2
#   - bad_cnt / total_rx <  0.001          -> P0-1 ❌ stall 不够，加倍再跑
#   - 若加到 200 clk stall 仍 pass          -> H1/H5/H6 至少一个是主根因，进 P0-3
```

### P0-2 若 P0-1 复现，跑分辨 H1 vs 蓝方假说

在 P0-1 复现的 tb 上：

```bash
# 实验 A：注入 H1 判据（把 f_wr_seq 换成 tag 常量）
#   在 la_ddr_ring_streamer.v 里加 `ifdef SIM_TAG_SEQ`
#   然后 `iverilog -DSIM_TAG_SEQ ...` 重跑
# 判据：
#   - 错值分布变成 0xA5/0x5A 主导 -> H1 是主根因，蓝方假说被证伪
#   - 错值分布不变            -> H1 排除，进实验 B

# 实验 B：注入 H5 判据（wbuf 清 LSB）
#   在 la_ddr_writer.v 里加 `ifdef SIM_MASK_LSB`
# 判据：
#   - 错值 0x01 计数掉到 0    -> H5 是次根因（至少覆盖 0x01 那一支）
#   - 无变化                 -> H5 排除，回到 P0-3
```

### P0-3 若 P0-1 复现且 H1/H5 都排除

**只有到这一步才允许考虑蓝方 partial-write 假说**。此时需要：

- 加更精细的 MIG 行为模型：write 数据入 write-buffer 后延迟 M clk 才 physically commit，
  期间 read 到同地址返回未提交数据。M ≈ 30-50 clk。
- 若在此模型下能复现，蓝方假说保留；且拟修 a) 应改为**"保持 M/burst*B 的安全距离"**
  而非固定 2*LENGTH*8。

### P0-4 若 P0-1 无论如何都 pass（bad_cnt 恒 0）

**说明 sim 环境根本覆盖不到上板机理**。此时：

- **禁止盲改 streamer**。
- 加 ILA 到上板 bit，抓 `wr_ptr_words / rd_ptr_words / avail_raw / stream_tdata / f_wr_seq
  / gb_word` 关键信号，触发条件 `stream_tdata != 8'h42`。
- 拿 ILA 数据 + 4.1 节 diff 打印回来重新分析。

---

## 6. 裁决

**⛔ 不允许蓝方进入 RTL 修改阶段**。

**必须先绿的关卡（顺序不可颠倒）**：

1. **P0-1 复现**：tb 增强后污染率 ≥ 0.1%（一个数量级低于上板但足以证明 tb 覆盖到了机理）。
   - **不绿就不能动 RTL**——包括 streamer 的 avail 阈值、writer 的 wr_ptr 时机、
     甚至看似"低风险"的 CSR 位。
2. **P0-2 或 P0-3 定根因**：H1/H5/H6/蓝方假说至少确定一个为主。
   - **多假说并存时禁止"同时改多处"**（AGENT.md §6 单变量递增）。
3. **写修复前先写 tb assertion**：目标是修完后 tb 里污染率归 0，回归可自动跑。
4. **修复上板前跑 tb baseline + 修复后 tb**——**逐字节相同**（除污染字节）才算 sim 层
   零回归。

**其它建议**：

- **P0-1 修改后跑一次 `tb_la_ddr_ring.v` 4/4 baseline**——确认新 stall 不打破原有
  P0e/P1/P2/P3 用例。若破坏，说明 stall 太激进，回退到较小值。
- **speed 账要实测**：加两个 CSR 计数字段
  - `writer_bytes_per_sec = (words_written * 16) / elapsed`
  - `drain_bytes_per_sec  = (words_drained * 16) / elapsed`
  上板抓 30 秒读一次差值。**这个数字比 sticky ring_overrun 有信息量 100 倍**，蓝方
  不给出这个数字就在讨论"writer > drain"是没依据的推断。
- **蓝方已 4 次综合上板**——按 AGENT.md §6 第 2 条，"一个方案试 2 次不成停下换思路"
  的红线早已越过。**这轮评审就是那次强制降速**。**不接受"再试一次综合"式响应**——
  下一轮蓝方回应必须带 P0-1 的 sim log 或 ILA 波形。

**允许的低风险动作（不需要红方进一步 gate）**：

- 加诊断 CSR（writer/drain rate、overrun_events 计数器带 clear、`send_pad_pulses`、
  `f_wr_seq_last`）——纯只读观测，不影响数据路径。
- tb 里加 `ifdef` 门控的 sim-only 注入。
- 上板加 ILA 抓 la_ddr_ring_streamer 的 `f_wr_data / f_wr_seq / f_wr_rtx` 与
  fpga_core_net 的 `tx_udp_payload_axis_tdata`。

**⛔ 依然禁止**（在 P0 未绿之前）：

- 改 la_ddr_ring_streamer.v 的 `burst_avail` / `avail_raw` / seq 计算路径。
- 改 la_ddr_writer.v 的 wr_ptr 更新时机。
- 改 la_ddr_writer.v 的 wbuf pack 逻辑。
- 加"看着能治" 的 pipeline 寄存器（`ddr3_wr_data <= wbuf[out_idx]` 前加一级 flop 之类）。

---

## 7. 附：本评审基于的证据强弱标注

| 论断 | 证据类型 | 强弱 |
|------|---------|------|
| wr_ptr 只在 W_DONE 更新 | RTL 直接可见 | 🟢 实测 |
| MIG UI read-after-write 保序 | UG586 §1.4 | 🟢 手册明确 |
| 错值 0x80/0x81/0x82 集中不符合 partial-write | 数值分析 | 🟡 强推断 |
| H1 (161-bit FIFO 位对齐) 是最可能主根因 | 错值 pattern 匹配 | 🟡 强推断，需 P0-2 验证 |
| ring_overrun sticky 不等于持续 lapping | RTL sticky 逻辑 + 无 clear | 🟢 实测 |
| writer 稳态 ≤ drain（100 MB/s vs 116 MB/s） | 时钟频率 + 网络上限 | 🟡 推断，蓝方需实测反驳 |
| 拟修 a) 在情况 α/β/γ 下都不解决问题 | 稳态队列分析 | 🟢 数值可算 |
| tb 4/4 pass 是因为 MIG 模型太理想 | 直接读 tb 源码 | 🟢 实测 |

**方法论合规性自检**：
- 单变量递增：本评审提出的每个替代根因都有独立可测量判据，不混合。
- 复现优先：明确要求 P0-1 复现在前，修复在后。
- 实测 vs 推断：所有关键论断都标注强弱。
- 复用成熟方案：tb 就地改，不新写；MIG 行为模型分级加 stall（先粗后精）。

