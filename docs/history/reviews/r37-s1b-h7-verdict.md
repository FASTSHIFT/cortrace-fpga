# r37 — S1b H7 假说 + F1 修复方向 红方裁决

**日期**：2026-09-04
**对象**：`reviews/r36-response-p0-results.md`（蓝方 P0-1/P0-2 结果 + H7 新假说 + F1 拟修）
**前置**：`reviews/r36-s1b-residual-race-analysis.md`（红方 gate）、
`stage4-datapath/21-nack-onboard-integration-plan.md` §3.4（S1b 现状）
**复核 RTL**：`la_ddr_writer.v`（W_DONE 分支 L225-238）、
`ddr3_wr_ctrl.v`（`ddr3_wr_done ← end_cmd_cnt` L174-181、`end_data_cnt` L134）、
`ddr3_generate_data.v`（S1a patgen：`app_wdf_data = {16{wr_data_cnt}}`）、
`la_ddr_ring_streamer.v`（`wr_words_committed` 接 `words_written`）、
更新后的 `tb_ddr_ring_fixed.v`（P0-1 stall 增强 + `SIM_MEM_INIT_11` 门控）
**立场**：严格证伪。蓝方这一轮的 sim 覆盖度和实验设计是合格的，但 H7 到"精确
形式（wr_ptr 更新早于 data commit）"之间还差**一个决定性单变量实验**；F1 放行但
必须走完加固后的 gate 才能上板。

---

## 一句话裁决

**✅ P0-1 复现达标（32.6% >> 0.1%）**；**✅ H2/H5/H6 被 `SIM_MEM_INIT_11` 实验决定性
证伪**；**⚠️ H7 得到强证实但精确形式（wr_ptr 早于 data commit）尚未与另外两个变体
（wbuf/out_idx race、streamer 读地址计算错）分辨——追加一个 sim-only assertion 实验
即可闭环**；**✅ 放行 F1（la_ddr_writer 层加 wait，不动 vendor `ddr3_wr_ctrl`）**，
但绑定 5 条 gate（sim 归零 → tb_la_ddr_ring 4/4 → 综合时序 → 上板 0x42 30 min → 上板
ramp 60 min），任何一条不绿都必须回 sim。**不放行"F1 + F2 同批改"或"跳过某条 gate"**。

---

## Q1 — P0-1 达标性

### 1.1 定量判据：**过**

- 阈值：r36 §5 P0-1 要求 ≥ 0.1%。
- 实测：`bad_cnt / rx_cnt = 8048 / 24720 = 32.6%`（🟢 实测）。
- 幅度超阈值 320×，稳定 `RESULT=FAIL_SCRAMBLED`。**tb 已覆盖到"上板出错但仿真不出
  错"的机理**，`WDF_STALL_CLK=40 + RDY_STALL_CLK=60 + RD_LAT 抖动` 是打开 race
  window 的最小充分组合。

### 1.2 "错值随环境变、机理相同"证据链——**基本可接受，但欠一次交叉对齐**

蓝方主张：
- 上板错值 = S1a bit patgen 遗留（0x01/0x80/0x81/0x82/0x7F/0x6A-0x6E）
- tb baseline mem init = DEADBEEF → 错值 = 0xDE/0xAD/0xBE/0xEF 各 2012 次（🟢 实测）
- tb SIM_MEM_INIT_11 → 错值 = 0x11 × 8048（🟢 实测）
- ∴ 错值 = "streamer 读到未 commit 的 DDR 位置" = 该位置的历史内容

**红方接受这个证据链的核心结论**（错值来源于 DDR 静态存储内容，不是 seq/rtx/wbuf
之类 RTL 内部状态 leak）。但**上板那一支的对齐没做严**，有一个 loose end：

- **S1a patgen 实际写入的字节分布**（`ddr3_generate_data.v` L169）：
  `app_wdf_data = {16{wr_data_cnt}}`，`wr_data_cnt` 是 8-bit 计数器 0..LENGTH-1。
  即 S1a 会把 DDR 铺满**每个 128-bit word = 16 字节相同值**的模式，且字节值随地址
  遍历 0x00→0xFF。**若 S1b 读到的是纯 S1a 遗留，字节直方图应该在 0x00-0xFF 上大致
  均匀**（每字节值出现 ~相同次数）。
- **上板实测直方图高度偏斜**（0x01=228K vs 0x80=67K vs 0x02=??? 未见）——**这与
  纯 S1a 遗留假说不完全一致**。可能的解释：
  - S1a bit 上电后只跑了少量 write/read loop（`error=0` 快速触发 halt），DDR 只被
    部分覆盖，剩下的位置是**上电随机态 + refresh 后趋于弱定态**（DRAM 上电特性）
  - S1b writer 已经覆盖了大部分地址，只剩**特定 offset 集**（比如每个 burst 末尾
    的 N 个 word）持续暴露 stale 内容——那 N 个 offset 对应的 S1a 字节值恰好是
    0x01/0x80 等（因为 `wr_data_cnt` 在 offset N 处的值）
  - S1b bit 上一次写入的残留（比如上一次跑 ramp 模式的 wrap 边界值）
- **不影响机理判定，但影响事故还原精度**。红方**允许**这条差异先记入 known-issue，
  不要求本轮解决——因为 `SIM_MEM_INIT_11` 单一实验已经**充分**证明"错值 = DDR 内容"
  的因果链；上板那支的具体值只是**佐证**，不是**判据**。

### 1.3 建议蓝方补一次 sim 交叉对齐（不阻塞放行 F1，但**必须做**）

再跑两个 sim 变体，5 分钟能出结果：

```bash
iverilog -DSIM_MEM_INIT_00 ...   # 错值应全部变 0x00 × 8048
iverilog -DSIM_MEM_INIT_FF ...   # 错值应全部变 0xFF × 8048
```

**判据**：若两次都是"单一值 × 8048"，则"错值 = DDR 内容"关系稳定，不受 mem-init
特殊性影响（0x11 那一位可能与 mem 打包地址、latch 时机有偶然对齐）。
**若出现 0x00 只 4000 次 + 其它值 4000 次**，说明**还有一支错值来自 RTL 状态 leak**
（H1/H5/H6 中某个未被 SIM_MEM_INIT_11 判据覆盖的机理），需回到 P0-2。

**这条不阻塞 F1 gate 1（sim 归零），但阻塞 F1 gate 4（上板压测）——F1 修完后
sim 里`SIM_MEM_INIT_00` / `SIM_MEM_INIT_FF` 都必须归零，才允许烧板。**

---

## Q2 — H7 假说裁决 + H2/H5/H6 覆盖

### 2.1 SIM_MEM_INIT_11 实验对各假说的证伪能力

| 假说 | 错值来源 | mem_init override 后预期 | SIM_MEM_INIT_11 结果 | 裁决 |
|------|--------|------------------------|--------------------|------|
| 蓝方原 (partial-write) | DRAM 未定义态 + 半 write | 应含 0xFF/0x00 混合 或不变 | 全 0x11 | 🟡 未完全否证（0x11 也可能是"半 write 后强制为 mem 现值"）|
| H1 (161-bit FIFO 位对齐 leak) | seq/rtx leak | 应该无变化（错值仍 = seq 值） | 全 0x11 | 🟥 **证伪** |
| H2 (packetiser race) | latched_seq/gearbox 状态 | 应该无变化 | 全 0x11 | 🟥 **证伪** |
| H5 (wbuf 综合 skew) | wbuf 里旧 word 数据 | 应该看到 0x42 或早期数据 | 全 0x11 | 🟥 **证伪** |
| H6 (divider 综合退化) | seq 字段错位 | 影响 seq 不影响 data byte 值 | 全 0x11 | 🟥 **证伪**（且 seq 类假说与 data 值无关）|
| **H7 (wr_ptr 早于 commit)** | 该 DDR 位置 stale 内容 | 应该完全变成新 init 值 | 全 0x11 | 🟢 **强证实** |

**结论：H7 类假说（"streamer 读到 stale DDR 内容"）保留；H1/H2/H5/H6 被证伪。**

### 2.2 H7 精确形式的三个变体——**尚未分辨**

蓝方 §5 把 H7 定为"wr_ptr 更新时机过早（W_DONE 依赖 `end_cmd_cnt` 不等 `end_data_cnt`）"。
**这是三个变体之一**，还有两个 rise-time 等价机理需分辨：

**H7-A（蓝方主张）**：`la_ddr_writer.v` W_DONE 转换看 `ddr3_wr_done`（= `end_cmd_cnt`
= 命令送达 MIG），但 `end_data_cnt`（= 写数据全送 MIG）可能滞后数十 clk。wr_ptr 提前
前进，streamer 用新 wr_ptr 判 avail 拉高，读到 mem 里 stale 内容。**证据：ddr3_wr_ctrl.v
L174-181 明确 `ddr3_wr_done ← end_cmd_cnt`**（🟢 实测 RTL）；tb 加 `WDF_STALL_CLK=40`
后 `app_wdf_rdy` 排队让 `end_data_cnt` 显著滞后（🟢 实测）。

**H7-B（wbuf/out_idx race）**：`la_ddr_writer.v` 里 W_DONE 后 `burst_ready <= 0`，
`out_idx` 复位准备下一 burst。但 vendor `ddr3_wr_ctrl` 可能还在 pop wbuf（因为 stall
让 wdf_wren 排队）。若 out_idx 在下 burst wrap 到 0 前 vendor 还没读完最后几个 word，
数据实际写入的**不是最后几个 word 的正确值**而是新 burst 的头几个 word。**证据：
`ddr3_wr_data = wbuf[out_idx]` 是组合读**（🟢 RTL 可见），tb 里 `word_idx` 和 `out_idx`
独立跑，加 stall 后两者不再同步。

**H7-C（streamer 读地址计算错）**：streamer R_START 时 `ddr3_rd_addr <= rd_ptr_words`，
但**如果 wr_ptr 前进后 avail 阈值刚好过线，streamer 读的是刚更新的 wr_ptr - LENGTH*8
到 wr_ptr 这一段**——若 writer 那 burst 数据还没到 mem，读到 stale。这与 H7-A 机理
一致，但强调**判 avail 时机**而不是 wr_ptr 前进时机。

**H7-A/B/C 都在 `SIM_MEM_INIT_11` 实验中给出相同结果**（错值 = mem 现值），因为三者
都表现为"streamer 读到 mem 里 stale 值"。**因此 mem-init 实验不能分辨三者。**

### 2.3 分辨 H7-A/B/C 的建议实验（必做，不阻塞 F1 但影响 F1 具体形式）

**实验 D（3-5 行 sim-only 代码，最强判据）**：

在 tb_ddr_ring_fixed.v 里加：

```verilog
// 监控 streamer 发 rd_start 时, mem[rd_addr] 是否已经是 0x42 (writer 已 commit)
// 还是 stale (writer 未 commit)
reg [31:0] rd_at_stale = 0, rd_at_committed = 0;
always @(posedge clk) begin
    if (rd_start) begin
        if (mem[rd_addr[15:3]] == 128'h4242_4242_4242_4242_4242_4242_4242_4242)
            rd_at_committed <= rd_at_committed + 1;
        else
            rd_at_stale <= rd_at_stale + 1;
    end
end
// 同时统计 end_cmd_cnt 与 end_data_cnt 的 skew
reg [31:0] cmd_first = 0, data_first = 0, same_clk = 0;
reg cmd_seen = 0, data_seen = 0;
always @(posedge clk) begin
    if (u_wrc.end_cmd_cnt && !u_wrc.end_data_cnt) begin cmd_first <= cmd_first + 1; end
    else if (u_wrc.end_data_cnt && !u_wrc.end_cmd_cnt) begin data_first <= data_first + 1; end
    else if (u_wrc.end_cmd_cnt && u_wrc.end_data_cnt) begin same_clk <= same_clk + 1; end
end
```

**判据表**：

| 观察 | 结论 |
|------|------|
| `rd_at_stale > 0` 且 `cmd_first > 0` | H7-A/C 成立（cmd 先于 data 到达，rd 蹭到 stale）|
| `rd_at_stale > 0` 但 `cmd_first == 0` | H7-B 成立（cmd/data 同步，问题在 wbuf pop 时机）|
| `rd_at_stale == 0` | ❌ H7 不成立，回 P0-2 找其它假说 |

**这个实验 15 分钟能加完、跑完。** 若结果落 H7-A/C，F1（改 W_DONE 转换条件）**精确
治本**；若结果落 H7-B，F1 无效，得改 la_ddr_writer 里 wbuf 与 out_idx 的解耦。

### 2.4 蓝方 §5 关于 `wr_words_committed = words_written` 的观察——正确

蓝方指出："`wr_words_committed` = `words_written` 也是 W_DONE 那一 clk 更新，同一 race"。
🟢 实测正确：`la_ddr_writer.v` W_DONE 分支同 clk 更新 `words_written` 和 `wr_ptr_words`。
所以 streamer 的 backlog 判定（`overrun_now = backlog > RING_CAP - LENGTH`）与
avail 判定（`avail_raw = (wr_ptr - rd_ptr) mod RING`）**共用同一个提前更新**，修 W_DONE
时机会同时修好两处，不需要两处改。

---

## Q3 — F1 放行

### 3.1 放行 F1（有条件）

**放行的 F1 精确定义**：

只改 `la_ddr_writer.v` W_DONE 转换条件：

```verilog
// before:  W_RUN: if (ddr3_wr_done) wst <= W_DONE;
// after:
W_RUN: if (data_done_latched) wst <= W_DONE;
```

其中 `data_done_latched` 由**监听 vendor `ddr3_wr_ctrl` 的 `end_data_cnt` 边沿**（或
等价信号 `app_wdf_wren & app_wdf_rdy & data_cnt==MAX_NUM`）拉起。

**红方对 F1 的边界要求**：

- ❌ **不改 vendor `ddr3_wr_ctrl.v`**（保守，vendor 件绕开风险）。
- ❌ **不加 "MIG write-drain 空" 额外 wait**（蓝方 §6 提到的"+ MIG write-drain 空"）。
  理由：UG586 §1.4 明确 MIG UI 对同一 controller 后续的 read 操作保序（read-after-write
  ordering）。**一旦 `end_data_cnt` 拉高，MIG 内部收下了所有 write data**，后续通过
  vendor rd_ctrl 发的 read cmd 对同地址会看到该 write。**若 F1 gate 1 不过，那时再考虑
  加固定 wait 计数器（8-16 clk）作为 belt-and-suspenders，不要一开始就过度保守**。
- ❌ **不同时改 la_ddr_ring_streamer.v**（保持单变量）。
- ❌ **不并入 F2**（streamer 保持 write-pipeline depth 安全距离）——F2 是**如果 F1
  不成功才启用的 fallback**，一次改一处。
- ✅ **可以加 CSR 只读诊断字段**：`writer_data_wait_cycles`（本 burst 从 `end_cmd_cnt`
  到 `end_data_cnt` 等了多少 clk）——这是 F1 有效性的**上板证据**。
- ✅ **必须加 sim assertion**：`W_DONE 那一 clk 起，mem[wr_addr - LENGTH*8..wr_addr-8]
  == 0x42×16`，作为回归测试的持久判据。

### 3.2 F1 验证顺序 gate（严格串行，不允许跳）

| # | Gate | 判据 | 不过怎么办 |
|---|------|------|-----------|
| G1 | sim `tb_ddr_ring_fixed` + stall (baseline mem=DEADBEEF) | `bad_cnt == 0` | 回改 F1 or 探 H7-B |
| G2 | sim `SIM_MEM_INIT_11` + stall | `bad_cnt == 0` | 同 G1 |
| G3 | sim `SIM_MEM_INIT_00` + `SIM_MEM_INIT_FF` 交叉对齐（§1.3）| 都 `bad_cnt == 0` | 存在残留 stale 源，非 F1 覆盖 |
| G4 | sim `tb_la_ddr_ring` 4/4 baseline（doc 19 P0e/P1/P2/P3）| `ALL_PASS` | F1 破坏了 P0e 或 NACK 路径，回改 |
| G5 | sim `tb_ddr_ring_fixed +DRAMP + stall` | `bad_cnt == 0` | ramp 独立回归失败，看 §3.3 |
| G6 | 综合（Vivado）| `WNS ≥ 0.1 ns, TNS = 0` | 长路径出现，加 pipeline 或减 wait 深度 |
| G7 | 上板 30 min `src_fixed=1`（0x42）| PC 侧 `bad = 0`；`ring_overrun` 起始不再 sticky | 回 P0-3（更精细 MIG 模型）|
| G8 | 上板 60 min ramp（`src_fixed=0`）| `_rampcheck.py` 报 100% monotone | 同 G7 |

**顺序不可颠倒**：G1-G5 都是 sim，本地几分钟；G6 综合 4 分钟；G7-G8 上板 90 分钟。
**任一 gate 不绿都必须停下**（AGENT.md §6"一个方案试 2 次不成停下换思路"）。

### 3.3 需回归的具体测试列表

**sim 层**：

- `tb_ddr_ring_fixed` × {baseline, SIM_MEM_INIT_11, SIM_MEM_INIT_00, SIM_MEM_INIT_FF} × {WITH_MIG_STALL=0, 1} × {default, +DRAMP} = **16 组** run，每组 `bad_cnt == 0`。
- `tb_la_ddr_ring` 4/4 TEST A/B/C/D（doc 19 P0e + P1 concurrent R/W + P2 NACK 重传
  + P3 nack_fail 越界）——**这是 F1 不打破 NACK 语义的守门测试**。TEST C（seq 5..7
  重传范围）尤其关键：F1 让 wr_ptr 晚更新，`wr_words_committed` 也晚更新，重传窗口
  检查 `rtx_written = ((rtx_abs_word + rtx_words_left) <= wr_words_committed)` 的
  判定会跟着变——**跑通 TEST C 才能证明 F1 没让 NACK 语义漂移**。
- `tb_la_ddr_writer` 独立 baseline（如果存在）——writer 单独回归。

**上板层**：

- G7：`src_fixed=1` 30 min，`stream_grab -o cap.bin`，`grep -c $'\x42'` == 总字节。
- G8：`src_fixed=0` 60 min，`_rampcheck.py cap.bin` 报 monotone-mod-256 无破损。
- **额外记录**：读 CSR `writer_data_wait_cycles` 平均值——若稳态在 30-50 clk 附近，
  与 sim `WDF_STALL_CLK=40` 数量级一致，是 F1 挠对痒的实测证据。

**"tb_la_ddr_ring 4/4 baseline 不打破"是硬门槛**——doc 19 §14 的所有离线论证依赖它。

### 3.4 F1 上板残留污染的 fallback 政策

| 上板结果 | 允许的下一步 |
|---------|-----------|
| `bad = 0`（30 min 0x42 + 60 min ramp）| ✅ 关闭 S1b，进 S2（真 trace 接 writer 输入）|
| `bad_rate < 0.001%`（残留噪声）| 🟡 允许 F2（streamer 保 `2*LENGTH` 安全距离）**但先回 sim 复现次要机理**——把 stall 参数加大直到 sim 出现残留，用同一实验方法定位 |
| `bad_rate 0.001% - 0.1%` | ⚠️ **停下，回 P0**——F1 只覆盖了 H7-A/C，H7-B 或未识别机理仍在。禁止直接叠加 F2 |
| `bad_rate > 0.1%` | ⛔ F1 无效，回改，参考 §2.3 实验 D 结果重新定 F1 具体形式 |

**关键红线**：不允许"F1 + F2 一次改完再上板"。这是 AGENT.md §6 单变量递增的直接
要求。**F2 只在 F1 上板留下**明显但小的**残留时才启用**，且启用前必须先在 sim 里
复现该残留——否则 F2 是猜盲。

### 3.5 不放行"跳过 §3.2 gate"的替代方案（红方对 fallback 的明确路径）

若蓝方觉得 G1-G8 太重，红方给两个**可缩短但不减损**的替代路径，任选其一：

**路径 α（并行 sim gate，串行上板 gate）**：G1-G5 用 make -j4 并行跑（~5 分钟），
G6 综合一次拿数（~4 分钟），G7-G8 串行（~90 分钟）。总时长 ~100 分钟。**允许**。

**路径 β（先 30 秒上板嗅探）**：G1-G4 过后，**允许**先 30 秒上板抓样看
`bad_rate`——如果 30 秒就 ≥ 0.1% 说明 F1 上板不对，不用等 30 min。**但 G3/G5/G6
必须补跑**，30 秒嗅探只是提前止损，不替代 gate。

### 3.6 拒绝的 F1 变体

**不放行**：
- ❌ F1 + 同时改 `la_ddr_ring_streamer` 的 `burst_avail` 阈值（多变量）。
- ❌ F1 + 改 vendor `ddr3_wr_ctrl` 让 `ddr3_wr_done` 挂 `end_data_cnt`（动 vendor 件，
  影响面无法界定）。**若真要改 vendor，必须先给出一份 diff review 单独走一轮红方
  评审**——因为它影响所有用同一 ctrl 的 top（S1a `trace_ddr_selftest`、S2/S3/S4）。
- ❌ F1 + 加不确定长度的 wait 计数器（例如"等 100 clk"这种拍脑袋值）。若加 wait，
  必须**先跑 §2.3 实验 D 拿到实测 `cmd_first / data_first` 分布**，wait 至少覆盖
  99 percentile skew。

---

## 4. 方法论合规性核查

| AGENT.md §6 要求 | 蓝方本轮表现 | 红方本轮 gate |
|-----------------|-----------|-------------|
| 实测/推断标签 | ✅ 直方图、bad_cnt、mem-init 结果都标 🟢 实测 | ✅ 各表格标注 |
| 单变量递增 | ✅ H1 判据只改 f_wr_seq，H7 判据只改 mem init | ✅ F1 只改 W_DONE，不并 F2 |
| 复现优先于修复 | ✅ P0-1 未过前没动 RTL | ✅ 8 条 gate 全 sim 优先 |
| 一个方案试 2 次不成停下 | ✅ H1 一次证伪即停 | ✅ 上板 fallback 明确停下阈值 |
| 复用成熟方案 | ✅ tb 就地改，ifdef 门控 | ✅ 不动 vendor 件 |
| 别把假设当事实 | 🟡 §5 "H7 是根因"表述略偏——精确形式尚未证 | 🟢 §2.2 拆 H7-A/B/C，实验 D 分辨 |

**总体合规**。唯一需蓝方在下一轮提交时收紧的：**别把 H7 说成"根因"，说成
"strongly-supported hypothesis pending experiment D"**——H7-A/B/C 未分辨前，"根因"
两字给早了。

---

## 5. 蓝方下一步动作清单（按顺序）

1. **补 §1.3 交叉对齐**：跑 `SIM_MEM_INIT_00 / SIM_MEM_INIT_FF`。（🕓 5 min）
2. **跑 §2.3 实验 D**：加 `rd_at_stale / rd_at_committed / cmd_first / data_first`
   计数，确认落在 H7-A/C 还是 H7-B。（🕓 15 min）
3. **写 F1 diff**：`la_ddr_writer.v` 只改 W_RUN → W_DONE 转换条件，加 sim assertion。
   diff 提交给红方过一遍**再综合**。（🕓 30 min + red review）
4. **走 §3.2 G1-G8**：sim 五组 + 综合 + 上板两组。（🕓 100 min）
5. **写 F1 结果**（`r37-response-f1-results.md`）：附各 gate log，请求红方 r38 关闭
   S1b。

**禁止**：跳步、并行改多处、上板前跳 sim gate、不带交叉对齐直接烧板。

**允许**：`writer_data_wait_cycles` CSR 只读诊断字段随 F1 一起加（观测手段，无副作
用）；`SIM_MEM_INIT_*` 门控留在 tb 里作为长期回归判据；把 §2.3 实验 D 的 4 个计数
器也保留（`ifdef SIM_H7_PROBE`），未来任何 wr_ptr 时机相关改动都能复用。

---

## 6. 附：证据强弱标签

| 论断 | 证据类型 | 强弱 |
|------|---------|------|
| P0-1 复现率 32.6% | tb 直接输出 | 🟢 实测 |
| SIM_MEM_INIT_11 → 错值全 0x11 | tb 直接输出 | 🟢 实测 |
| H1/H2/H5/H6 被证伪 | mem-init override 判据 | 🟢 实测（H1）+ 🟡 逻辑推断（H2/H5/H6）|
| H7 (streamer 读到 stale) 类假说保留 | mem-init override 判据 | 🟢 实测 |
| H7-A vs H7-B vs H7-C 未分辨 | mem-init 判据对三者等价 | 🟢 逻辑分析 |
| `ddr3_wr_done ← end_cmd_cnt` 不等 data commit | ddr3_wr_ctrl.v L174-181 | 🟢 RTL 实测 |
| MIG UI read-after-write 保序前提 `end_data_cnt` 已拉过 | UG586 §1.4 + 蓝方 §5 | 🟡 手册 + 逻辑推断（未做 MIG 内部 ILA 验证）|
| S1a patgen 写 `{16{cnt}}` | ddr3_generate_data.v L169 | 🟢 RTL 实测 |
| 上板错值 pattern 与 S1a 均匀分布不完全一致 | 上板直方图 + patgen RTL 对比 | 🟢 实测 + 🟡 推断 |
| F1（改 W_DONE 转换）是精确治本 | H7-A 假设下逻辑推断 | 🟡 待实验 D 证实 |
| F2（streamer 加安全距离）是治标 | r36 §3 稳态分析 | 🟢 数值可算 |

**红方本轮结论 = 放行 F1，但绑定 8 gate + 交叉对齐 + 实验 D + 单变量红线**。
蓝方过完这几关，S1b 可关闭进 S2。
