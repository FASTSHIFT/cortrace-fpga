# 30 — 根因锁定：DDR 数据通路 byte-lane 3 延迟 2 个字（不是 IDDR，不是时序）

日期：2026-09-10

## 结论（决定性）

长期的"变化数据解不出、静态图案完美"的根因是：**128-bit DDR 数据通路里第 3
号字节 lane（byte position 3 mod 16）比其余 15 条 lane 延迟了整整 32 字节
= 2 个 DDR 字**。其余 15 条 lane 完全正确（delay 0）。

这不是 IDDR 采样问题，不是信号完整性，不是 CDC FIFO 丢字节。

## 怎么定位的：帧化 PRBS 压测

关键实验设计（用户建议加固定同步帧）：

1. **降频排除时序**：TRACECLK 56M→10.2M（固件 `PLL_R_OVR=22`，sysclk 不变），
   坏率几乎不变 → 逻辑 bug，非时序。
2. **ramp 排除大部分传输链**：clk200 域 +1 ramp 经 DDR→UDP，223MB 零错——但它
   在 la_ddr_writer 的字节打包**之后**注入，覆盖不到 IDDR 和 byte-lane 打包。
3. **帧化 PRBS 精确定位**（CSR 0x0D，`trace_capture_a7` 内 trace_clk 域）：
   在 **trace_clk 域**生成 xorshift32 PRBS，走**与真实 IDDR 采样完全相同的
   CDC FIFO + DDR ring + gearbox + packetiser + UDP**，只跳过 IDDR primitive 本身。
   每 8192 字节一个块，块首 8 字节固定 marker + PRBS 重播种，主机据此锁定并逐字节比对。

## gdb 级证据（每 lane 延迟测量）

```
lane : best byte-delay (got[i]==ref[i-d])
   0..2 : delay=0   match=59/59
   3    : delay=32  match=59/59   <-- 唯一异常
   4..15: delay=0   match=58/58
```

lane 3 的字节 = `ref[i-32]`，即它输出的是 **2 个 128-bit 字之前**同一 lane 的
旧数据。命中率 59/59 完美，说明是确定性的固定延迟，不是随机丢字节。

**复现两次，坏 lane 的 payload 位置会变（第一次=3，第二次=7）**：因为 marker
(blkpos=0) 落在相对 DDR 16 字节字栅格的不同相位（抓取窗口起点不 16 对齐）。
不变量是：**每个 128-bit DDR 字里恰好有一个固定的物理字节位置，输出的是 2 个字
之前的旧值**；其余 15 个字节位置完全正确。position 3 vs 7 只是 payload 索引相对
物理字位置的相位差。

这个"某一物理字节位置延迟 2 个字"的特征，指向 **wbuf 突发缓冲 / ddr3_wr_data
组合读 out_idx / MIG app_wdf 的某个字节 lane 多打了流水**，而不是 IDDR。

## 为什么之前所有"干净"测试都测不出

- **静态 TPIU 图案（AA/55、walking-1）**：每个字都一样，lane 3 延迟 2 个字
  读出的还是同一个值 → 完全隐形。这就是"采集链 100% 干净"的假象来源。
- **真实 ETM trace**：每字节都在变，lane 3 拿的是 2 字前的旧字节 → 每 16 字节
  错 1 个，正是"变化数据坏、静态数据好"的精确特征。
- **A-sync 长零游程**：对单 lane 错位最敏感，所以最先崩。

## 根因（仿真确认）：la_ddr_writer 打包器 late-sample 组合字

用 `tb_prbs_cdc.v`（真实 axis_async_fifo + 真实打包器，纯功能仿真、零时序/零
亚稳态）**在仿真里 1:1 复现了硬件症状**：FIFO 裸弹出的字节流完全正确，但打包成
128-bit 字后，**每个字里 cap_bidx==9 那个字节被延迟 2 个字（32 字节），got=ref[i-32]**，
mod16 恒为同一位置。→ 故障不是时序、不是 CDC 亚稳态，是**打包器纯逻辑 bug**。

bug 在 `la_ddr_writer.v`：
```
wire [127:0] cap_word_full = cap_word_next;   // 组合
// FIFO 用 cap_word_valid（寄存器，晚一拍）当 tvalid 采样 cap_word_full
```
`cap_word_valid` 是寄存器，比第 16 个字节晚一拍拉高。在它拉高那一拍，如果又来了
第 17 个字节（`cap_valid_in` 高——正是数据经 trace_clk→cap_clk CDC FIFO 连续
背靠背流入的稳态），`cap_word` 已经把第 17 字节移入，`cap_word_next` 变成
bytes[1..16]+byte17，而不是 bytes[0..15]。存进 FIFO 的 128-bit 字因此错位，
一个字节位置带着 2 个字之前的旧值。

**为什么 ramp 测不出**：ramp 在 clk200 域注入，受 DDR 背压/间隙节流，`cap_valid_in`
不是连续高，几乎不触发"完成拍又来一字节"的碰撞；PRBS 经 CDC FIFO 以 200M 连续
背靠背弹出，每个字都碰撞。

## 修复

在完成拍（`cap_bidx==WORDS_PER-1`）把整字锁进 `cap_word_latched` 寄存器，
`cap_word_full` 改接这个寄存器而非组合的 `cap_word_next`。仿真验证：
`FIX_LATCH` 版 packed 流 0 错。

改动：`la_ddr_writer.v`（+ `tb_prbs_cdc.v` 复现/回归仿真）。

## 硬件回归结果（lane 修复已验证）

- 帧化 PRBS（`iddr-prbs 1`）：修复前每 16 字节坏，修复后干净跨度内 0 错。
- **真实 56M trace，A-sync 坏率 87.7% → 27.1%**；cortrace 解码从 786KB 处 fatal
  推进到 **7.7MB**（10×），begin/end 平衡 305617 对、max depth 11、解出真实函数。

## 仍存在的第二个缺陷（burst 边界 ±1024 错位）

PRBS 还暴露了一个独立的次级 bug，真实 trace 也受影响（残余 27% 坏 async / 7.7MB
处 fatal 的来源）：

- **低速（10M）表现**：ring streamer 把每个 1024B burst 发两遍（相邻包字节完全相同，
  seq 仍 +1）。纯低速 ring 饥饿：drain 快于 slow writer 填充。56M 下 packet 重复=0。
- **56M 表现**：偶发 burst 边界错位——某包头部 32 字节（=2 个 128-bit 字）来自
  "下一 burst"，随后回跳 **−1024**（正好一个 burst）到正确位置。signature：
  `pkt = ref[a:a+32] 然后 ref[a-1024:...]`。
- 嫌疑：`la_ddr_writer` 的突发 staging（`wbuf`/`out_idx` 组合读 `ddr3_wr_data`，
  W_RUN→W_DONE 的 out_idx wrap）或 streamer R_NEXT→R_START 边界多吐 2 个字。
- 行为级 MIG 仿真（tb_la_ddr_ring TEST E, CAP_DIV=40）**未复现**——触发点在真实
  MIG 读时序（`app_rd_data_end`/`ddr3_rd_data_vld` 节拍），需真实时序或更精确的
  MIG 模型才能仿真定位。

## 标准方案：用成熟 IP 替换手搓打包器（已做）

不再手写"移位打包 + 独立 async FIFO"，改用 verilog-ethernet 的
`axis_async_fifo_adapter`（S_DATA_WIDTH=8 → M_DATA_WIDTH=128），一个经过验证的
块同时完成 **8→128 位宽转换 + cap_clk→ui_clk CDC**，由它自己管 tvalid/tkeep，
从根上消除"数据 vs valid 差一拍"这类 bug。注意 adapter 是小端（首字节进 LS lane），
下游是大端，故对 128 位输出做字节反转保持线上字节序不变。

硬件验证（`trace_ddr_stream_stdip.bit`，56M PRBS）：
- **lane-3 skew 消失**：不含 burst 边界毛刺的包 100% 逐字节正确（每 16 字节错误清零）。
- **低速重复消失**：相邻包重复 = 0。
- 回归：`tb_la_ddr_ring` A–E 全过。

## 仍存在：−1024 burst 边界跳变（第二 bug，独立于打包器）

标准 IP 修好了打包器，但暴露出**另一个独立缺陷仍在**：约一半的包，开头 2 个字
（32 字节）来自"下一个 burst"（payload_ref 位置 +? ），随后回跳 **−1024**（正好
一个 burst/一个 packet）到正确位置。signature：`pkt = ref[s:s+32]` 然后
`ref[s-1024+32:...]`。1750/3500 非跨界包中招。

这不在打包器（已用标准 IP），在 **DDR ring 读路径**（`la_ddr_ring_streamer` /
`ddr3_rd_ctrl`）：每个读 burst 的头 2 个 beat 疑似是上一次读残留/预取。方向：
查 `ddr3_rd_data_vld`（=valid&end）与 streamer R_RUN 首拍捕获的关系，读数据流水
延迟是否让首 2 拍落到错误 burst。

## −1024 bug 已在仿真确定性复现（tb_la_ddr_ring TEST F）

用**位置编码源**（每字节 = 全局 word 序号低 8 位，每 16 字节 +1）跑 tb_la_ddr_ring
TEST F，behavioural MIG **复现了**该 bug：drained 字节的 word-staircase 出现
195 次向后跳，第一次在 byte 1057，每 2048 字节（=2 个 burst）一次。

glitch 数值：
```
@byte 1057: prev=word65 now=word1
@byte 3105: prev=word129 now=word65   (+2048)
@byte 5153: prev=word193 now=word129
```
即每个 burst 实际吐了 **66 个 word（= LENGTH 64 + 2）**，多出的 2 个 word 是"下一
burst 的头"，然后回跳重读——正是 HW 上"包头 32 字节（2 word）来自下一 burst、
随后 −1024 回跳"的根源。

## 精确嫌疑：streamer word 计数用 valid&end，rd_ctrl 用 valid

`ddr3_rd_ctrl`：`ddr3_rd_data_vld = app_rd_data_valid & app_rd_data_end`，而其
内部 `rd_data_cnt` 只按 `app_rd_data_valid` 计数、`end_data_cnt` 在 cnt==63 结束。
`la_ddr_ring_streamer` R_RUN 却用 `ddr3_rd_data_vld`(=valid&end) 捕获 f_wr_data 并
计 words_drained。两个计数口径（valid vs valid&end）不一致，在 burst 边界让 streamer
多捕获/错位 2 个 beat。这是"每 burst 多 2 word"的最可能来源。

## 真正根因（TEST F 探针定位）：writer 单缓冲 wbuf 生产/消费竞争

TEST F 加探针后排除了读侧（读地址严格 +512 连续、读字数恰好 64）和字数（写/读每
burst 都恰好 LENGTH）。写侧探针一击命中：偶数 burst 正确，**奇数 burst 只有头 2 个
word 是新数据，其余是上一个 burst 的残留**（如 burst#1 写 64,65 后是 2 而非 66）。

根因：`la_ddr_writer` 的 **单个 `wbuf[]` 数组** + `burst_ready`/`burst_done`
握手有生产/消费竞争——在低填充率下，一个 burst 在其缓冲只刷新了头几个 word 时就被
提交，复用了上一 burst 的尾部。这正是 HW 上"包头 2 word + −1024 回跳"的来源。

## 修复：ping-pong 双缓冲（标准方案）

用两个 bank（`wbuf0/wbuf1`）+ 两个 1-bit 序列指针（`fill_seq`/`commit_seq`，
差值 = 占用 bank 数 0/1/2）。生产者填一个 bank 时，写 FSM 提交另一个 bank，二者
**永不索引同一数组**，从根上杜绝半刷新提交。占用满(=2)时反压。

验证：
- 仿真 `tb_la_ddr_ring` **TEST F backward-word-steps = 0**（已转为 gating），A–E 全过。
- **硬件 56M PRBS：per-packet 校验 54907/54907 逐字节完美，0 坏包。**
- **真实 trace A-sync 坏率 87.7% → 27.1% → 0.0%**；cortrace 从 786KB 处 fatal →
  **完整解完 19.8MB 零 fatal**，2,666,520 对 begin/end 平衡，mismatched=3、dropped=0。

## 三个 bug 全部解决（回顾）
1. lane-3 skew（打包器 late-sample）→ 换标准 IP `axis_async_fifo_adapter`。
2. 低速整-burst 重复（ring 饥饿）→ 与 #3 同源，双缓冲后消失。
3. −1024 burst 重排（writer 单缓冲竞争）→ ping-pong 双缓冲。

## 现存改动（未 commit）
- `trace_capture_a7.v`：加 `test_src_en` + 帧化 xorshift32 PRBS 源（诊断用，
  CSR 0x0D 门控；正常抓取时 =0，走真实 IDDR）
- `trace_ddr_stream_top.v`：CSR 0x0D `iddr_prbs_125` 接线
- `trace_ctrl.py`：`iddr-prbs` 子命令；`prbs_check.py`：帧化校验器
- 固件：`PLL_R_OVR=22`（10M 诊断态）、boot 默认 selftrace——**这两个是临时诊断态，
  查完要回退到 R=4 / 正常**
