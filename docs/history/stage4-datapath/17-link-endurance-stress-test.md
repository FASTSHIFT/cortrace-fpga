# 17 — STM32→FPGA→网口→PC 全链路长时压测设计

**日期**：2026-08-22
**目标**：验证完整 trace 链路（STM32 ETM → TPIU 物理引脚 → FPGA 采集 → UDP self-TX →
网口 → PC）在**长时间**（小时级）运行下的丢包/误码稳定性，量化各段贡献。
**工具**：`scripts/stream_endurance.py`（本次新增，为小时级设计，内存恒定）。

---

## 1. 为什么需要专门的长时压测

之前的验证都是秒级：
- `probe stream-link`（ramp）：573MB / 5s，旁路 TPIU/引脚，只测网络通路。
- `stream_recv.py`：**把整份抓样存进内存**再落盘，115MB/s 下跑几分钟就吃光 RAM，
  不能小时级。

长时压测要回答秒级测不出的问题：**热漂移 / SSN 累积 / 时钟抖动 / 主机调度长尾 / 计数器
回绕**下,链路会不会出现缓慢的丢包漂移或偶发大 burst。

---

## 2. 三个被测对象（分开测，别混）

| 被测 | 数据源 | 覆盖链路 | 用途 |
|------|--------|----------|------|
| **A 网络链路耐久** | FPGA 内部 ramp（CSR 0x09） | FPGA→UDP→PC（旁路 TPIU/引脚） | 网络/PC 侧能不能长时零丢，字节可预知可逐字节校验 |
| **B 全链路耐久** | 真 ETM trace（STM32 跑 CoreMark） | STM32→TPIU→引脚→FPGA→UDP→PC | 用户要的完整链路 |
| **C 物理采集图形** | TPIU 内建图形 W1/W0（CURTPM） | STM32 TPIU→引脚→FPGA→PC | 真引脚 + 可预知变化字节，校采集眼/误码 |

- **A** 用 ramp：速率恒定，把"网络/PC 跟不跟得上"与"ETM 突发"解耦——**A 若零丢，说明
  链路本身没问题**；B 的丢包就归因到 ETM 突发或采集前端。
- **B** 是最终目标，但真 trace 有 ETM 突发（burst），秒级已见 ~0.007-0.1% 偶发 seq-gap
  （§4.2 实测），需长时看是否稳定在这个量级、有无漂移。
- **C** 用 walking 图形（W1/W0，单 lane 热点轮转，可预知）走真引脚，秒级即可，验证
  4 条 data lane 采样无误码；不用长时（图形恒定，长时无新信息）。

---

## 3. 判据（三个丢包通道，别只看一个）

1. **wire seq-gap**：每包 4 字节 BE 序号的不连续（含 uint32 回绕处理）。抓 UDP/网络丢包。
2. **capture-side lost_cnt**：FPGA clk200 侧 FIFO 溢出计数（`DEPTH+34..37` via :5001）。
   抓采集前端溢出。⚠️ **本计数当前不可信**（见 §5 坑），需先修 depth 偏移再启用。
3. **decode 完整性**：抽样一段（`--sample`）跑 opencsd，确认 INSTR_RANGE 正常、PC 100%
   落 flash——证明链路交付的是**可解码的有效 trace**，不只是字节。

**零丢包判据**：A 必须 seq-gap=0；B 允许 ETM-burst 造成的极小 seq-gap（量级需稳定、
不随时间漂移），且抽样必须解得出。

---

## 4. 执行步骤

### 4.0 前置（每次必查，见 AGENT.md §2 最高频坑）
```bash
ip -br addr show <收流网卡>          # 必须持有 .245！ens33 可能漂成 .246
sudo ip addr add 192.168.10.245/24 dev <收流网卡>   # 若没有
sudo sysctl -w net.ipv4.conf.<收流网卡>.rp_filter=0
sudo sysctl -w net.core.rmem_max=268435456          # 256MB，避免内核缓冲丢
```
LED 判据：led0(TRACECLK) + led1(网络TX) 都闪 = 链路活。

### 4.1 A 网络链路耐久（ramp，先跑，锁定基线）
```bash
# 切 FPGA 到内部 ramp
python3 scripts/trace_ctrl.py --ip 192.168.10.42 stream-selftest 1
sudo python3 scripts/stream_endurance.py --iface <网卡> --hours 1 --interval 30
python3 scripts/trace_ctrl.py --ip 192.168.10.42 stream-selftest 0   # 记得关
```
判据：整程 seq-gap=0。若非零 → 网络/PC 侧问题（rmem、调度、线缆），先解决再做 B。

### 4.2 B 全链路耐久（真 trace，用户目标）
```bash
# STM32 ETM 必须开着 + CURTPM 清零（见 AGENT.md §4.4）
# 关 SysTick 减少中断污染（可选，长跑更干净）：openocd mww 0xE000E010 0
sudo python3 scripts/stream_endurance.py --iface <网卡> --hours 8 \
     --interval 60 --sample /tmp/endurance_probe.bin
# 结束后抽样解码：
cd decode && python3 opencsd_etm4_run.py /tmp/endurance_probe.bin <elf>
```
判据：seq-gap 量级稳定（不随小时数上升）；抽样解码 INSTR_RANGE 正常、PC 100% flash。

### 4.3 C 物理图形（可选，校采集眼）
```bash
python3 scripts/trace_doctor.py probe tpiu-pattern --patterns W1,W0
```

---

## 5. 已知坑 / 注意

- **capture-side lost_cnt 现不可信**：30s 实测读出 27 亿且每 interval 涨 ~3000 万——不是
  真溢出数，是 `--depth` 偏移（默认 61440）对这个 stream bit 不匹配、或 :5001 在满速流下
  返回了别的寄存器。**启用前需核对 stream bit 的 DEPTH+34..37 到底映射什么**；当前以
  wire seq-gap + 抽样解码为准。
- **:5001 poll 会被满速 :5555 流淹**（AGENT.md 坑点 25）：`stream_endurance` 的 lost_cnt
  poll 每 interval 才打一次、best-effort，超时就跳过，不影响收流（收流从不被 poll 阻塞）。
- **真 trace 的 ETM 突发**：B 的极小 seq-gap 曾被归因到源头 FIFO 溢出，但 §6.3（r36 修正）
  证伪了该结论——**丢帧位置未坐实**，ramp 结构上不源头丢，poll 抢占未隔离。动 DDR 前先跑
  §6.4 的证伪实验（P-1 不 poll ramp / P0d 平均率实测）。DDR 架构见
  `18-ddr-backpressure-architecture.md`（前提未证前冻结）。
- **uint32 序号回绕**：~110k pps 下 seq 每 ~10.8 小时回绕一次，`stream_endurance` 已按
  32-bit delta 处理；超 10 小时的跑不受影响。
- **内存**：`stream_endurance` 默认丢弃 payload，内存恒定；`--sample` 只留前 N MB。
  绝不要用 `stream_recv.py` 做长跑（它全存内存）。

---

## 6. 实测结果（2026-08-22）

### 6.1 数据

| 测试 | 时长 | 数据量 | 速率 | wire seq-gap | 坏帧 |
|------|------|--------|------|-------------|------|
| B 真 trace | 180s | 20003 MB | 111 MB/s | 80 events / 2532 帧 = **0.013%** | **0** |
| B 真 trace + CPU pin | 180s | 20003 MB | 111 MB/s | 74 events / 2641 帧 = **0.0135%** | **0** |
| A ramp（恒速）| 60s | 6955 MB | 116 MB/s | 29 events / 1149 帧 = **0.017%** | **0** |

抽样 8MB 每次都解码正常（真 trace INSTR_RANGE 56206、PC 100% flash；ramp 逐字节 monotone
0 破损）。**全链路交付的字节零误码。**

### 6.2 是丢帧不是坏帧（`_seqdiag.py` 30s / 330 万包）

- **纯丢帧（frame loss）**：8 次 forward-jump，0 次 reorder，0 次坏长度（全 1028B）。
- **成段丢**：一次丢 1/4/5/9/10/16/45 帧——典型缓冲瞬时溢出特征，不是零星单帧。
- **坏帧率 = 0**：到达的帧字节全对（NIC `errors=0`、UDP `InCsumErrors=0`、ramp 逐字节验证）。

### 6.3 分层定位：丢帧位置尚未坐实（r36 修正，勿信旧结论）

> ⚠️ **本节初版结论"丢在 FPGA 源头"已被红方 r36 + RTL 复核证伪，见下。**

逐层查内核计数器（主机侧）：

| 层 | 计数器 | 结果 |
|----|--------|------|
| 收流进程 | CPU pin + chrt 实时优先级 | 丢包不变（0.013%→0.0135%）**但 pin 是否真生效未验证** |
| socket buffer | UDP `RcvbufErrors` | 0（读取时机未与流量窗口对齐，存疑）|
| UDP 层 | UDP `InErrors`/`InCsumErrors` | 0 |
| NIC/驱动 | `rx_dropped`（backlog=250000 后）| 新增 0 |

**初版据此反推"帧没被 FPGA 发出来 → 源头 FIFO 溢出"——这条推断被证伪：**

1. **ramp 结构上不可能在源头丢帧**（RTL 实锤，`trace_stream_top.v:615,652`）：selftest/ramp
   模式 producer 被 FIFO 背压门控（`bw_ready = fifo_in_ready & selftest_active`），且 drop
   计数器带 `& ~selftest_active` 不计。**ramp 溢不出源头 FIFO**。所以 ramp 的 0.017% seq-gap
   **绝不是源头丢**，必是主机侧或 CSR poll 抢占——初版"ramp 也丢→源头缓冲太小"的推理作废。
2. **漏查了多个丢包点**：`/proc/net/softnet_stat`（软中断 backlog，110k pps 高发）、
   `tc -s qdisc`、`IgnoredMulti`（之前非 0=5967 未解释）、`netstat -su` 全量——都没查。
   "主机全环节零丢"是在只查 4 个计数器下下的，不成立。
3. **CSR poll 抢占未隔离**：`stream_endurance` 每 interval 发 :5001 lost_cnt 请求，走与
   self-TX **共享的 TX 路径**（`fpga_core_net` 仲裁 RX-echo 优先）。**每次 poll 可能让
   self-TX 暂停一下**，正好造成积压/丢帧。而"上次 ramp 573MB 零丢"是不 poll 的单次抓，
   这次 60s 有 poll——**poll 是两次唯一没控制的变量**。

### 6.4 结论（r36 修正版）

- ✅ **到达帧字节零误码**（0 坏帧，`_seqdiag` + ramp monotone 实测）——但普适性未验证
  （采集坏包率会 0.75%→10% 时变，坑点 17/21），需在高坏包率窗口重测。
- ⚠️ **未达零丢包**（~0.01-0.02% 丢帧），但**丢帧位置未坐实**——不能断言"FPGA 源头"。
  ramp 结构上不源头丢，故至少 ramp 那部分丢在主机/poll 侧。
- **动 DDR RTL 前必做的证伪实验**（全软件/0 成本，见 r36 §b/§c）：
  1. **P-1**：关掉 :5001 poll 跑纯 ramp——若不 poll 就零丢，则丢是 poll 自造，源头无问题。
  2. **P0a** 补查 softnet_stat/qdisc/netstat -su/IgnoredMulti + 验证 pin 真生效。
  3. **P0b** 换 recvmmsg 批量收流——判"是不是单线程 recvfrom 瓶颈"。
  4. **P0c** 修 lost_cnt 读法拿到直接源头丢帧数（RTL 是对的，见 §6.5）。
  5. **P0d（决定 DDR 生死）** 实测 CoreMark 平均 trace 净荷率 vs 线速——§6.1 平均已 111MB/s
     距线速仅 6%，DDR"平均<线速则零丢"前提濒危。
- **软件调优（rmem/pin/backlog）已试到顶但结论不硬**（pin 生效性未验、单线程瓶颈未排）。

### 6.5 lost_cnt 读值坏——是读法不是 RTL（r36 复核）

- **RTL 正确**（`trace_stream_top.v:651-660`，红方读过）：`lost200`/`drop = cap_valid &
  ~fifo_in_ready & ~selftest_active`/CDC/映射 NB+34..37，实现与接线都对。
- **坏值真因 = host 读法**：(a) `read_lost_cnt` 假设 `DEPTH=61440`，若运行 bit 的 DEPTH 不同，
  offset 指向别的寄存器；(b) 满速 :5555 流下走共享 :5001 读会超时/读到 stale。
- **P0c 修读法即可**（核对运行 bit DEPTH + 暂停 self-TX 再读或用独立低频端口），**一行 RTL
  不动**。修好后能直接量化源头丢帧，替代 §6.3 的主机计数器反推。

### 6.6 P-1 + 间隔计时实验：真凶是 USB 网卡（AX88179）静默丢帧（2026-08-22）

按 r36 跑完 P-1 及后续实验，三个假设（源头 FIFO / poll 抢占 / egress-ARP）全部证伪，
真凶收窄到主机 USB 网卡。

**实验数据：**

| 实验 | 结果 | 排除的假设 |
|------|------|-----------|
| P-1：ramp + **不 poll** 60s | 仍丢 100 events / 3577 帧（0.052%）| **poll 抢占**（r36 假设）✗ |
| 分层计数器 delta（ramp 30s）| NIC `rx_dropped`=0、softnet CPU7 `dropped`=0、UDP `RcvbufErrors`=0、`InErrors`=0 | 主机各层计数器均未记录丢帧 |
| UDP InDatagrams delta | +3.428M ≈ seqdiag 收到 3.423M | 主机收到的 = 上来的 |
| **间隔计时**（`_gaptiming.py`，12 gaps）| **12/12 gap 的到达间隔正常（~2µs，0 pause）** | **egress/self-TX 停顿**（ARP/HDR 超时）✗ |
| ARP 抓包（30s 窗口）| FPGA(.42) ARP **0 次**、总 ARP 0 次 | **ARP 刷新/deadlock** ✗ |

**关键判据——间隔计时**：若丢在 FPGA egress（ARP/HDR 超时 backoff），缺口后的下一包会
**晚到**（数百 µs 到 8ms 的停顿）。实测 **12/12 缺口处下一包都准点到（~2µs=正常包距）**，
流全程满速不停顿，只是**少了几个序号**。→ 帧是在流不停顿的情况下"凭空消失"的。

**"帧凭空消失 + 无停顿 + 主机任何计数器都不记" = 静默丢在 USB 网卡硬件/驱动层。**
- 网卡 = **ASIX AX88179**（USB `0b95:1790`，USB3.0 5Gbps 链路，`ax88179_178a` 驱动）。
- AX88179 在持续千兆 RX 下有众所周知的丢帧问题，且部分丢帧**不进 `rx_dropped` 计数**
  （丢在 USB 传输/芯片内部 buffer，内核不可见）。
- **速率依赖佐证**：同一网卡同一链路，AGENT.md §2 记录 **dock 直连 58MB/s → seq-gap=0
  （零丢）**；本次 **111-116MB/s → 0.01-0.05% 丢**。只有速率变了。**丢帧是 AX88179 在
  ~115MB/s 撞到它自己的持续吞吐天花板**，不是 FPGA、不是协议、不是我们的软件。

### 6.7 修正后的结论与方向

- **坏帧 = 0**（字节零误码，已达标，多次实测）。
- **丢帧根因 = 主机 USB 网卡 AX88179 在 ~115MB/s 静默丢**，**不在 FPGA 源头、不在
  self-TX egress、不在 UDP/socket、不是 ETM 突发、不是 poll**（全部实测证伪）。
- **DDR 缓冲方案（doc 18）对这个丢帧无效**——丢在主机网卡，FPGA 加缓冲救不了主机收不下。
  DDR 冻结判据（平均<线速）本就濒危，现在更明确：**当前瓶颈是主机侧 NIC，不是 FPGA 缓冲**。
- **真正的修法方向（按成本从低到高）**：
  1. **换网卡**：用主板板载千兆（非 USB）或 Intel/Realtek PCIe 网卡——大概率直接零丢
     （USB NIC 是唯一变量）。**最便宜、最可能一击命中，优先。**
  2. **降速率到 AX88179 能稳的区间**：2-bit trace（速率减半到 ~58MB/s，已知零丢）或降频。
  3. recvmmsg / AF_PACKET 零拷贝——但丢在 NIC 硬件层，软件收流优化未必救得了。
- **下一步**：换一块非 USB 网卡（或主板板载口）重测 115MB/s 全链路，验证 seq-gap=0。
  若换卡即零丢 → 物理层链路（FPGA→网口）本身零丢零坏达标，问题纯粹在主机网卡选型。