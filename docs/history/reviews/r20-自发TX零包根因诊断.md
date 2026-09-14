# r20 · 自发 UDP TX「零包」根因诊断（对抗性）

> 症状：`selftx_test_top`（STREAM=1）烧板后 **tcpdump 抓 FPGA MAC = 0 包，连 ARP request 都没有**，:5001 echo 也哑。同一个 `fpga_core_net` 在 `swo_stream_top`（STREAM=0）网络完全正常。
> 立场：红方。已读 `fpga_core_net.v`（TX 路径全字段来源）。
> 核心质问（你自己提的）：是不是在用"代码审查说应该工作"对抗"板上零包"——矛盾时信哪个？

---

## 0. 先回答元问题：信board，不信代码审查

**板上零包是 ground truth。你列的"已查证"清单——FSM 行为桩仿真稳定发包、arp.v 会主动发、udp_ip_tx 不死锁——全是代码阅读 + 行为级仿真的产物。**

r15/r16 已经确立过同一个教训：**行为桩仿真（零延迟、无真实 link、理想 MAC）结构上测不出 link/时钟/复位/真实 MAC 的问题**。"selftx_fsm_tb 稳定发包"恰恰是**对 A 类问题（link 没起来）天然失明**的那种证据——它在一个没有 PHY、没有 RGMII 时钟、没有真实复位序列的世界里跑。

**所以：别再加代码审查。代码审查在"板上零包"面前是弱证据。** 现在唯一有价值的动作是**在板上做一个能把 A/B 干净分开的实验**。

---

## 1. 零包（含零 ARP + echo 哑）最可能的根因排序

### 关键线索解析：**"连 ARP request 都没有"** 把概率往哪推

这是最强的线索，你没充分用它。逐层推：

- **ARP broadcast 不需要解析到 dest MAC**——它 *就是* 解析过程（dest=ff:ff:ff:ff:ff:ff）。只要：① link up + MAC TX 能发 ② udp_ip_tx 收到一个带**有效 dest_ip** 的 hdr_valid 且 cache miss → ARP request 必然广播出去。
- 所以 **零 ARP** 只能是两类原因之一：
  - **(A) 物理层根本没发**：link 没起来 / MAC 没出复位 / RGMII TX 时钟没配 → 连广播都发不出。
  - **(C) FSM 的 hdr_valid 没带着有效 dest_ip 到达 udp_complete**：header 字段没接对 → udp_ip_tx 要么没收到 valid、要么 dest_ip=0 不去解析。

→ **注意：你设想的 B（"卡在 ST_HDR 等 hdr_ready"）单独并不能解释零 ARP。** 如果 FSM 真的把 hdr_valid + 正确 dest_ip 送进了 udp_complete，那么"卡在 ST_HDR 等 hdr_ready"的同时**ARP request 应该已经广播出去了**（hdr_ready 不来正是因为 ARP 还没解析完）。**卡 ST_HDR 但零 ARP，自相矛盾——除非 header 字段没接对（C）或根本没 link（A）。** 这是你 FSM 死锁推理里的漏洞。

### 排序（按后验概率）

| 排名 | 根因 | 依据 | 为什么 |
|---|---|---|---|
| **1** | **A：新顶层 link/时钟/复位没起来** | 全静默（零 ARP + echo 哑）= layer-1/2 征兆；`selftx_test_top` 是**从没验证过 link 的新顶层** | swo_stream_top 是已知好的参照，新顶层与它的差异（MMCM 哪几个 CLKOUT、复位拉伸、PHY reset、**RGMII TX 的 clk90/USE_CLK90**）正是 bringup 反复流血的坑——`fpga_core_net` 注释自己写了 USE_CLK90 一配错就"no TX" |
| **2** | **C：header 字段没完整 mux**（dest_ip/length/payload 仍连 RX 派生值）| 读 `fpga_core_net.v`：echo 路径下 `tx_udp_ip_dest_ip=rx_udp_ip_source_ip`、`tx_udp_dest_port=rx_udp_source_port`、`tx_udp_length=rx_udp_length`、payload 来自 RX FIFO——**全部来自收到的包** | 自发 TX 时没有 RX，这些值是 0/stale。FSM 拉了 hdr_valid 但 dest_ip=0 → ARP 不发、或 length=0 → 畸形帧不发。**这正好解释零 ARP**，而你只改了 valid/ready 没提 dest/length 怎么来 |
| **3** | **B：self_busy 永真把 echo 屏蔽**（你的假设）| `rx_udp_hdr_ready = (...) && !self_busy`，self_busy=(st!=IDLE)，IDLE 在 stream_tvalid=1 立刻进 ST_HDR | **能解释 echo 哑**，但**解释不了零 ARP**（见上）。所以 B 至多是"echo 哑"的原因之一，不是"零包"全貌 |

**最可能：A（新顶层 link 没起）或 A+C 叠加。** 你的 B 单独不成立——它解释 echo 哑但不解释零 ARP。

---

## 2. 审你的 FSM 死锁推理

**你的链条**："stream_tvalid 写死 1 → 上电立刻 self_busy 永真 → echo 永久屏蔽" —— ✅ **这条自洽**，能解释 echo 哑。

**你的延伸**："如果 ST_HDR 的 hdr_valid 没被接受 → 卡 ST_HDR → self_busy 永真 → echo 永哑 → **连 ARP 都不发**" —— 🔴 **这条不自洽**。

漏洞在最后一环：**"卡在 ST_HDR" ≠ "不发 ARP"**。如果 FSM 在 ST_HDR 把 `tx_udp_hdr_valid=1` 且 dest_ip 正确地送进了 udp_complete，那么 udp_ip_tx 会去解析 dest MAC → cache miss → **广播 ARP request**。`hdr_ready` 迟迟不来的**原因恰恰是 ARP 正在解析中**。所以：

- **卡 ST_HDR + 有 ARP** = 自洽（在等 ARP 解析）→ 但你看到的是零 ARP，所以不是这种。
- **卡 ST_HDR + 零 ARP** = 只可能因为 **hdr_valid 没真正到 udp_complete，或 dest_ip=0**（C），**或 MAC 根本不发（A）**。

**结论：你的 FSM 死锁推理解释了 echo 哑，但"零 ARP"这个症状把根因推向 A（没 link）或 C（header 没接对），而不是你描述的 B（纯 FSM 时序卡死）。** ARP 那条路径在 header 接对 + link 起的前提下**无论 hdr_ready 来不来都该发**——它没发，说明问题在它上游（link 或 header 连接）。

---

## 3. 最便宜的一刀切实验

### 你提的"给 selftx 加回 echo 看通不通"——❌ 不是干净的判别器

因为 **B 的机制（self_busy 永真）本身就会杀掉 echo**。所以"echo 哑"无法区分 A（没 link）和 B（self_busy 屏蔽）——两者都让 echo 哑。**这个实验的变量没隔离。**

### 干净判别器：**禁用 FSM，只测 echo**

在 `selftx_test_top` 里**把 self-TX FSM 摘掉**——最简单：`STREAM=0` 重新综合该测试顶层（或强制 `self_busy=0` 且 FSM 不抢 TX）。这样移除了 self_busy 这个变量：

- **echo 通了** → 新顶层的 link/时钟/复位**没问题** → 根因在 FSM/header（B 或 C）。
- **echo 仍哑** → 新顶层 **link 就没起**（A），与自发 TX 无关 → 去查 MMCM CLKOUT / 复位 / RGMII USE_CLK90 / PHY reset，对着 swo_stream_top 逐行 diff。

**这一刀直接把 A 和 (B/C) 分开**，因为它消掉了 self_busy 对 echo 的干扰。

### 比重新综合更便宜的前置（0 rebuild，先看）

1. **PHY link LED**：板上 RGMII PHY 的 link/act 灯亮不亮。灭 → PHY 层 link 都没起，强烈指向 A，连综合都不用重跑。亮 → PHY link 起了，但**不代表 fabric 侧 MAC 出了复位**（A 仍可能在 fabric 侧），所以灯亮还得做上面的 STREAM=0 实验。
2. **`led_reg` 那个调试灯**：`fpga_core_net` 把首字节放 LED（`assign led = led_reg`）。selftx 顶层若保留了它，看它有没有动——能旁证 TX payload 路径有没有活动。

**顺序：先看 PHY link LED（0 成本）→ 再跑 STREAM=0 echo 实验（1 次综合，干净分 A/B-C）。**

---

## 4. 方向要不要叫停（优先级 + timebox）

**要。这是 r19 Q5 警告的优先级倒挂的现行犯。**

- r19 已经判定：自发 TX 的优先级**在并口 SI 命门之后**，且 r19 Q4/Q6 给了**不需要自发 TX 的更稳路径**——用 PC 端轮询 UDP 读出（已验证 0.000%，doc 14 §31）拼流，喂 `orbuculum -f` **命名管道**。
- 更关键：**r19 的结论是"地基（orbuculum 能不能吃我们的字节格式）还没用干净 raw.bin 钉死"**。在地基没验之前做自发 TX，是**在没确认下游能解的前提下，先建上游推流管道**——顺序反了。万一 orbuculum 根本不吃我们的裸 TPIU 字节（r19 Q1 存疑），这个自发 TX 白做。

**timebox 建议**：
- **§3 的 STREAM=0 实验立刻做（今天）**——它不仅是 debug，还可能直接证明"自发 TX 这条根本不用现在趟"。
- **若 STREAM=0 echo 通（=新顶层 OK，bug 在 FSM/header）**：给 FSM 修 header mux（C）**最多再半天**。半天不通 → 砍，退回 PC 轮询拼流 + 命名管道（r19 已验证可行的路）。
- **若 STREAM=0 echo 也哑（=A，新顶层 link 没起）**：这是个纯 bringup 坑，**别在 selftx 新顶层上耗**——直接放弃这个独立测试顶层，回到 `swo_stream_top`（已知网络好）里加 STREAM 参数，复用它验证过的时钟/复位/RGMII 配置。**不要为了"独立测试"重新趟一遍 link bringup。**

---

## 5. 单一 next action + timebox

**Next action（单一、最低成本）**：
1. 先瞄一眼 **PHY link LED**（0 成本）。
2. 然后在 `selftx_test_top` 里 **`STREAM=0` 重新综合，只测 :5001 echo**——这一刀干净分开"新顶层 link 坏（A）"和"FSM/header 坏（B/C）"。

**为什么是这个而不是你提的"加回 echo"**：你的版本在 self_busy 还在的情况下加 echo，self_busy 永真会杀 echo，无法区分 A 和 B。**必须把 FSM 摘掉**才能干净测 link。

**Timebox**：
- 这一刀 + 后续修复 **总共给到半天**。
- 半天不通 → **砍自发 TX，退回 PC 端轮询 UDP 拼流 → orbuculum -f 命名管道**（r19 已论证可行且更稳）。
- 且无论如何，**先用干净 raw.bin 把 orbuculum 能不能吃我们格式钉死（r19 的那一刀，0 RTL）再说**——那个比自发 TX 更该先做，因为它决定整条流式路线成不成立。

---

## 点破

**你正在用"代码审查 + 行为桩仿真说应该工作"对抗"板上零包"的事实——而当这两者矛盾时，板永远对。** 你的三条"已查证"（FSM 仿真稳定、arp.v 会发、udp_ip_tx 不死锁）全是在"link 已经起、MAC 已出复位"的隐含前提下成立的代码逻辑；而板上零包（连 ARP 都没有）恰恰在质疑这个前提本身。**零 ARP 是最硬的线索，它说"TX 物理路径或 header 连接有问题"，而你设想的 B（纯 FSM 时序卡死）解释不了它。** 别再读代码找"为什么应该工作"，去板上做 STREAM=0 那一刀，让板告诉你 link 到底起没起。
