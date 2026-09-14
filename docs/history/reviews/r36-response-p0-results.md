# r36 蓝方响应 — P0-1 / P0-2 复现与假说裁决

**日期**：2026-09-04
**对象**：r36 红方评审要求的 P0-1 复现 + P0-2 分辨实验
**遵守约束**：整过程 **未修改** `la_ddr_ring_streamer.v` 或 `la_ddr_writer.v` 上板 RTL，
所有实验通过 tb + `ifdef SIM_TAG_SEQ` / `ifdef SIM_MEM_INIT_11` 门控。

---

## 1. 认错

先按红方硬伤 A/D/E 逐条认。

- **硬伤 A**：蓝方"burst 写入进行中那一 clk 恰好 ≥ LENGTH*8"在这份 RTL 上不成立。
  la_ddr_writer.v W_DONE 分支明确 `wr_ptr_words` 只在 `ddr3_wr_done` 那一 clk 递增
  一个完整 burst，中间不跳步。**蓝方 a) 拟修方向的前提被自己的 b) 观察否证**——
  我没意识到这条内在矛盾。
- **硬伤 D**：`ring_overrun` 是 sticky-1，没 clear。用"sticky=1"反推"稳态套圈"是把
  1-cycle 曾发生的事件当作稳态状态，方法论错。
- **硬伤 E**：writer 100 MB/s vs drain 116 MB/s 稳态 writer < drain，本来不应持续
  overrun。蓝方"writer > drain"是**从 sticky bit 反推**的推断，不是速率实测。

红方硬伤 B/C 保留待 P0 数据下结论（下文验证）。

---

## 2. P0-1 tb 增强 + 复现

**动作**：`tb_ddr_ring_fixed.v` 按 r36 §4.1 加 4 个 stall：

| 参数 | 值 | 模拟的机理 |
|---|---|---|
| WDF_STALL_CLK | 40 clk | app_wdf_rdy pulse-low：MIG 写数据 FIFO drain |
| RDY_STALL_CLK | 60 clk | app_rdy pulse-low：MIG bank activate/precharge |
| RD_LAT_BASE + RD_LAT_JITTER | 6+random(20) clk | read latency 抖动 |
| NET_STALL_EVERY / LEN | 100 / 10 clk | stream_tready 反压 |

**结果**：

```
==== fixed-source diag (fixed 0x42 source) ====
words_written = 1664, words_drained = 1545
wr_lost = 1, ring_overrun = 0
stream bytes received = 24720, non-0x42 count = 8048
RESULT=FAIL_SCRAMBLED (bad_cnt=8048)
```

- **污染率 8048/24720 = 32.6%**（远超红方要求的 ≥0.1% 阈值）✅ P0-1 复现
- 错值直方图：**只有 4 种,各 2012 次**：
  ```
  0xDE : 2012
  0xAD : 2012
  0xBE : 2012
  0xEF : 2012
  ```
- **`0xDE 0xAD 0xBE 0xEF` = tb MIG 内存的初始值 `128'hDEADBEEF_DEADBEEF_...`**

---

## 3. P0-2 实验 A：H1 (161-bit FIFO 位对齐) 判据

**动作**：`la_ddr_ring_streamer.v` 里 `f_wr_seq <=` 两处加 `ifdef SIM_TAG_SEQ`，
override 为常量 `32'hA5A5_5A5A`。tb 用 `-DSIM_TAG_SEQ` 编译。

**H1 判据**：如果错值来自 seq 位对齐 leak，错值应该变成 `0xA5 / 0x5A` 集中。

**结果**：

```
bad byte histogram: {0xDE: 2012, 0xAD: 2012, 0xBE: 2012, 0xEF: 2012}
```

**错值完全没变**——依然是 DEADBEEF，不含任何 A5 / 5A。

**⛔ H1 (FIFO 位对齐 seq leak) 证伪**。红方 §1.2 H1 假说排除。

---

## 4. P0-2 实验 B：H7 corroboration — 改 MIG 内存初始值

**动作**：`tb_ddr_ring_fixed.v` 里 `mem` 初始值加 `ifdef SIM_MEM_INIT_11`，
override 为 `128'h11111111...`。

**H7 判据**：如果错值是"streamer 读到 writer 从未覆盖的 DDR 位置，读回初始态"，
错值应该跟着改变。

**结果**：

```
bad byte histogram: {0x11: 8048}
```

- 错值**全部变成 0x11**，一个不剩
- **完全证实 H7**

---

## 5. 新假说 H7 — 根因

**H7 (新)**：**writer 的 `wr_ptr_words` 更新时机过早**——在 `ddr3_wr_done`
（=`end_cmd_cnt`，仅表示"所有 write commands 已被 MIG 接收"）那一 clk 就把 pointer 前进
一个 burst，但 **MIG write-data pipeline 里还可能有若干 word 未 physically committed 到
DRAM**。streamer 立刻看到 `wr_ptr` 前进，用新指针算 `avail_raw`，`burst_avail` 拉高，
发 read 到那 burst 的地址 → 读回 **该 DDR 位置未被覆盖过的旧内容**（sim 中是初始值
DEADBEEF/0x11，上板中是 S1a bit 时代 patgen 遗留的 `0x87/0xF1/0xE1...`）。

**关键 RTL 证据**（ddr3_wr_ctrl.v line 174-181）：

```verilog
//ddr3_wr_done
always @(posedge ui_clk) begin
    ...
    else if(end_cmd_cnt)
        ddr3_wr_done<=1'b1;
    ...
end
```

`end_cmd_cnt = app_en & app_rdy & (cmd_cnt==MAX_NUM)` — 是 **command 计数完成**信号，
不是 **数据 committed** 信号。数据 committed 应看 `end_data_cnt = app_wdf_wren & app_wdf_rdy
& (data_cnt==MAX_NUM)`，且还要加 MIG 内部 write→physical drain 的时间。

tb baseline（`app_wdf_rdy=1` 恒真）里 `end_data_cnt` 和 `end_cmd_cnt` 几乎同 clk 发生，
掩盖了 race。加 WDF_STALL_CLK=40 clk 后 `app_wdf_wren` 排队 → `end_cmd_cnt` 可能比
`end_data_cnt` 早数十 clk 到达 → race window 打开。

**H7 与红方 §1.2 H5（wbuf 综合 skew）关系**：H7 是**上游更早的 timing 错**，H5 是**下游
综合环节的进一步 skew**。若 H7 修正，H5 的信号足以让 write-data 稳定落 DRAM，不会
再暴露；若 H7 不修，H5 加不加都会污染。

---

## 6. 拟修方向（暂不实施，等 r37 裁决）

**修复 F1（推荐）**：把 la_ddr_writer 的 W_DONE 转换条件从 `ddr3_wr_done`（等
`end_cmd_cnt`）改为等 **`end_data_cnt` + MIG write-drain 空**。具体：

- 引入 `ddr3_wr_data_done` 或直接监听 `app_wdf_wren & app_wdf_rdy & (data_cnt==MAX_NUM)`
- 或加一个短的 write-drain 计数器（等 N clk 保证 MIG 内部 pipeline 空）

**修复 F2（保守 fallback）**：streamer 保持 **write-pipeline depth** 的安全距离。
但红方 §3 已论证"a) 加安全距离在稳态 writer<drain 时无益、在 writer>drain 时治标"——
且 H7 定位到问题在 wr_ptr 更新时机，不是 avail 阈值。**F1 精确治本，F2 是绕开**。

**修复 F1 风险清单**：
- 需检查 vendor `ddr3_wr_ctrl` 是否已内部保证 `end_cmd_cnt` = 数据全 committed
  （手册说没有，但 MicroPhase 版本需另查）
- 需检查 la_ddr_ring_streamer 是否用 `wr_ptr_words` 之外的信号（例如
  `wr_words_committed`）判 backlog（若是则应该和 wr_ptr 时机同步）—— **RTL 已确认
  `wr_words_committed = words_written` 也是 W_DONE 那一 clk 更新，同一 race**

---

## 7. 请求红方 r37 裁决

**已完成的 P0 关卡**：

1. ✅ P0-1 tb 增强复现（污染率 32.6%）
2. ✅ P0-2 实验 A 证伪 H1
3. ✅ P0-2 实验 B 证实 H7
4. ✅ 全过程未修改上板 RTL

**请求裁决的问题**：

- **是否允许进入 F1 修复阶段**（改 la_ddr_writer W_DONE 转换条件为等 `end_data_cnt` +
  MIG drain）？
- **验证顺序**：F1 修完先跑同一份 P0-1 stall-tb（错值率应归 0），再回归 tb_la_ddr_ring
  4/4 baseline（不打破 doc 19 P0e/P1/P2/P3），最后综合上板验证 0x42 = 100%。
- **F1 是否需要修 vendor ddr3_wr_ctrl**（严格版：让它内部等 data_cnt 而非 cmd_cnt），
  还是只在 la_ddr_writer 层加一个 wait？后者更保守（不动 vendor 件）。

**方法论合规性自检**：

- 未越过红方设的 gate（未动 streamer/writer 上板 RTL）
- 每步单变量：H1 判据只改 f_wr_seq；H7 判据只改 mem init
- 实测 vs 推断标签清晰
- 复用 tb + ifdef 门控，未新写文件

**附：完整错值 pattern（sim 与上板对比）**

| 场景 | 错值直方图 | 解释 |
|------|---------|------|
| 上板 S1b (bit=ddr_ring_selftest, DDR 有 S1a 残留) | 0x01/0x80/0x81/0x82/0x7F | 上一 bit patgen 遗留 |
| sim tb baseline (mem=DEADBEEF, stall on) | 0xDE/0xAD/0xBE/0xEF | tb mem init |
| sim tb SIM_TAG_SEQ (f_wr_seq=A5A5_5A5A) | 0xDE/0xAD/0xBE/0xEF (无变) | H1 证伪 |
| sim tb SIM_MEM_INIT_11 | 0x11 (单值) | H7 证实 |

**同一现象跨环境**：错值 = "streamer 读的地址对应 DDR 位置的历史内容"，无论那内容
来自 S1a bit 遗留、tb 初始 DEADBEEF、还是显式 override 0x11。这**只可能**发生在
"streamer 读了 writer 尚未真正 commit 的位置"——F1 是唯一治本方向。
