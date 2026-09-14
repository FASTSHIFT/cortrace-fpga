# FPGA 可观测性与调试架构设计：从"黑盒猜谜"到"错误码可追踪"

> 日期：2026-07-05
> 状态：设计提案（待评审 + 实施）
> 动机：H743 ETM 上板调试中，我们花了十余轮才定位到"self-TX FSM 死锁"这一个根因——不是因为问题难，而是因为**FPGA 内部状态几乎不可观测**。本提案把这次教训固化成一套可观测性架构。
> 关联：HANDOFF §7.1（self-TX/ARP 死锁）、proposal 29（ETM 溢出根因）、proposal 22/26（采样前端）

---

## 0. 一句话结论

**当前 FPGA 是个"黑盒"：出了问题只能靠外部现象（LED 慢闪、抓到 0 包）反推，内部状态机、FIFO 水位、错误计数几乎全部丢弃或不可读。** 本提案设计一套**分层可观测性**：① 廉价常驻的"状态/错误码寄存器堆 + 双读出通道（UART & UDP）"，② 按需插入的 ILA，③ 结构化的错误码与"首错锁存 + 时间戳"机制，让"没 trace 时到底卡在哪一级"能在几秒内定位，而不是几小时。

---

## 1. 现状盘点：我们到底能看到什么（几乎啥也看不到）

对 `fpga_core_net.v` / `trace_mmcm_stream_top.v` 现有可观测性做了审计：

| 内部状态 | 现状 | 后果 |
|---|---|---|
| self-TX FSM (`st`: IDLE/HDR/SEND/BACKOFF)、`hdr_timeout`、`backoff_cnt` | **完全不可读**（只在 RTL 内部） | 这次死锁根因,查了十几轮才靠"readout 能回但 stream 不发"间接推出 |
| MAC/IP/UDP 的 error 端口（`tx_fifo_overflow`、`rx_fifo_overflow`、`rx_error_bad_frame`、`ip_rx_error_invalid_header`…）| **大量 `.port()` 空接,直接丢弃** | RX 帧错、FIFO 溢出这些一手证据全扔了 |
| `dbg_rx_good_frame`/`dbg_rx_bad_fcs`/`dbg_tx_axis_tvalid` | 有 tap,但**只驱动 LED**,不可精确读 | 只能看闪灯猜,无计数无时间 |
| trace 采样：MMCM lock、cap_valid、FIFO 水位 | 仅 `clk90_locked` 1 bit + `lost_cnt` 经 :5001 可读 | 无法区分"GPIO 没信号 / MMCM 没锁 / 去帧错 / FIFO 满" |
| UART | `uart_txd = 0` **硬接地,没用** | 网络挂了就完全失联 |
| ILA/VIO | **整个工程一个都没有** | 想抓波形得临时插 + 重综合(慢) |

**核心痛点**：可观测性**又少又散又易失**——出问题时第一反应是"抓 0 包",但**0 包可能是 7 个不同环节中任意一个坏了**：
```
GPIO 无信号 → MMCM 没锁 → 采样错 → 去帧错 → FIFO 溢出 → self-TX FSM 卡 → PHY/网络断
```
现在要靠排除法一个个试,每次试都要 halt/resume + 抓包 + 可能触发新死锁。**这就是低效的根源。**

---

## 2. FPGA 业内是怎么提高调试效率的

综合业内常见做法,分四层（从常驻到按需、从粗到细）：

### 2.1 常驻状态寄存器堆（Status/CSR register file）—— 最高性价比
把每个关键模块的**状态、计数器、首错锁存**汇聚到一个统一编址的寄存器堆,通过一个**永远可用的旁路通道**（UART 或专用 debug UDP 端口）随时读。这是 SoC/网络芯片的标准做法（想想 PHY 的 MDIO 寄存器、以太网卡的统计计数器）。
- **关键**:这个读出通道必须**独立于被调试的数据通路**。我们这次的教训就是——数据流（self-TX）挂了,而 :5001 readout（另一条路径）还活着,正是它救了命。debug 通道要刻意做成"数据通路挂了它还能活"。

### 2.2 结构化错误码 + 首错锁存（sticky first-error + timestamp）
每个模块定义**错误码枚举**,出错时：
- **锁存首个错误码**（sticky,不被后续覆盖,只由显式清除/复位清掉）——因为"第一个错"才是根因,后面往往是雪崩。
- **记录发生时刻**（用自由运行的周期计数器打时间戳）。
- **累加各类错误的发生次数**。
这样"错误可追踪、可记录",而不是转瞬即逝。业内叫 "sticky status bits"（PCIe/以太网寄存器里到处是）。

### 2.3 关键 FSM 的"状态直读 + 卡死看门狗"
把每个状态机的**当前状态编码**接到状态寄存器堆;再给每个 FSM 配一个**看门狗计数器**——某状态停留超过阈值就置一个 `stuck` 错误码 + 锁存是哪个状态卡的。我们这次的 self-TX FSM 其实**已经有** `hdr_timeout`(8ms),只是①没暴露出来②超时后进 BACKOFF 重试而不报错,所以死锁被"静默吞掉"了。

### 2.4 ILA / VIO（按需,细粒度波形）
- **ILA**（Integrated Logic Analyzer）：片上逻辑分析仪,触发条件下抓 N 个周期的波形,经 JTAG 读回 Vivado 看。适合"寄存器堆看到某模块异常后,插 ILA 抓那个模块的信号级时序"。
- **VIO**（Virtual I/O）：JTAG 虚拟按钮/LED,运行时置位/读值,可用来手动触发、复位单个模块。
- 代价：占 LUT/BRAM + 需重综合 + 走 JTAG（我们的 FT232H+VMware 链路本身还不稳）。所以 ILA 是**二线手段**,一线应该是常驻寄存器堆。

### 2.5 其它业内常用
- **数据通路打点/校验**:已知图案注入（我们的 golden pattern / HSYNC 探针就是这类）、CRC/parity、序列号（UDP seq 已有）。
- **性能计数器**:FIFO 高水位记录（max occupancy）、吞吐计数、丢包计数（`lost_cnt` 已有雏形）。
- **可复现的仿真 testbench**:上板前把 FSM 死锁场景在 iverilog 里复现（本工程一期做过）。

---

## 3. 本项目的 DEBUG 模块设计

### 3.1 架构总览

```
        ┌─────────────────── 各功能模块 tap ───────────────────┐
        │ GPIO采样  MMCM   去帧  captureFIFO  self-TX FSM  MAC/UDP │
        │  ↓状态/错误 ↓lock  ↓err  ↓水位/溢出   ↓state/stuck  ↓err端口│
        └──────────────────────┬───────────────────────────────┘
                               ▼
                    ┌──────────────────────┐
                    │  dbg_regfile          │  统一编址的状态/错误/计数寄存器堆
                    │  - 状态直读            │  + free-running cycle counter(时间戳源)
                    │  - sticky first-error  │  + 首错锁存(码+时间戳+现场)
                    │  - per-error counters  │
                    └───────┬──────────┬─────┘
                            │          │
              ┌─────────────▼──┐   ┌───▼──────────────┐
              │ UART console   │   │ UDP debug :5003  │   两条独立读出通道
              │ (板载串口,     │   │ (request/reply,  │   (数据流挂了仍可用)
              │  网络挂也能用) │   │  复用现有 readout)│
              └────────────────┘   └──────────────────┘
```

### 3.2 错误码定义（结构化、分模块段位）

用 16-bit 错误码,高 4 位 = 模块 ID,低 12 位 = 该模块内的错误号。示例：

| 模块 ID | 模块 | 典型错误码 |
|---|---|---|
| 0x1 | GPIO/采样前端 | `0x101` 无 TRACECLK 边沿(N ms 内无翻转)、`0x102` MMCM 失锁 |
| 0x2 | 去帧/对齐 | `0x201` 长时间无 TPIU 同步、`0x202` parity/nibble 配对失败率超阈 |
| 0x3 | capture FIFO | `0x301` 溢出(lost_cnt++)、`0x302` 高水位告警 |
| 0x4 | self-TX FSM | `0x401` **HDR 状态卡死(ARP 未完成)**、`0x402` SEND 反压超时、`0x403` 反复 BACKOFF |
| 0x5 | MAC/PHY | `0x501` RX bad FCS 超阈、`0x502` TX FIFO 溢出、`0x503` PHY link down |
| 0x6 | UDP/IP | `0x601` invalid header、`0x602` early termination |

> 有了这套码,这次的死锁会**直接报 `0x401`**,而不是让我们对着"0 包"猜十几轮。

### 3.3 寄存器堆布局（示例,经 :5003 / UART 读）

| 地址 | 名称 | 内容 |
|---|---|---|
| `0x00` | `DBG_MAGIC` | 固定 `0xDB` + 版本,确认 debug 模块在线 |
| `0x04` | `CYCLE_LO/HI` | 自由运行周期计数器（时间戳基准,读时快照） |
| `0x08` | `FIRST_ERR_CODE` | **首错码(sticky)**,复位/显式清除才清 |
| `0x0C` | `FIRST_ERR_TIME` | 首错发生时的 cycle 计数 |
| `0x10` | `FIRST_ERR_CTX` | 首错现场（如 self-TX 卡在哪个 state、FIFO 水位快照） |
| `0x14` | `ERR_COUNT[n]` | 各模块错误累加计数 |
| `0x20` | `FSM_STATE` | 各 FSM 当前状态编码打包（self-TX st、采样 state…） |
| `0x24` | `FIFO_OCC` / `FIFO_MAX` | capture FIFO 当前 & 历史最高水位 |
| `0x28` | `TRACECLK_ACT` | TRACECLK 活动检测(近 N ms 有无边沿) + MMCM lock |
| `0x2C` | `RX/TX 计数` | good/bad 帧、发包数、lost_cnt |
| `0x30` | `GPIO_LEVEL` | **原始 trace 引脚电平快照** `{clk,d3,d2,d1,d0}` |
| `0x31..0x3A` | `GPIO_EDGES` | **原始引脚翻转计数**（TRACECLK + TRACED0..3，各 16-bit）|

> **原始 GPIO 监视器（P1 增补）**：直接在 clk125 域对 `trace_clk_in`/`trace_data_in` 双同步 + 边沿检测,**完全不经过采样 MMCM**。这是关键的**交叉验证**手段——引脚翻转计数 > 0 就证明"信号确实进到了 FPGA GPIO",从而把"引脚没信号"和"MMCM 没锁/解码错"彻底区分开。`fpga_health.py` 会据此给出定责结论：
> - 引脚全静止 → `raw trace pins STATIC`（STM32 ETM/接线/DAP 问题）
> - 引脚翻转但 MMCM 没锁 → `SAMPLING/FREQ issue`（TRACECLK 频率 vs 比特流,GPIO 侧没问题）
>
> **TRACECLK 频率计 + gap 检测器（P1 增补）**：频率计用固定 16.78ms 窗口数引脚边沿 → 实测频率（regfile 0xFF3B-3D）；gap 检测器数 TRACECLK 停顿 >8 clk 的次数 + 最长 gap（0xFF3E-41）。用于区分"频率不对"(freq)、"时钟断续导致 MMCM flap"(gap)、"频率对且连续但仍不锁"(需查 MMCM 复位/相位) 三种情况。

## 软件触发复位 + 自动重启（P1 增补,自愈）

调试中反复遇到"FPGA 卡死只能物理断电重插"的痛点。加了两条恢复机制,**不用断电**：

1. **软件触发复位**：`:5002` CSR 写 `REG_SOFTRST(0x10)` → 拉伸 256 clk125 周期的软复位,复位 trace 采集前端（MMCM + FIFO）+ 清 dbg_regfile sticky 计数。`fpga_health.py <ip> reset` 一键触发。用途：卡住时先软复位试图恢复,顺便清零 sticky 计数以获得干净读数（解决之前 sticky 计数无法清零、误导判断的问题）。
2. **自动重启看门狗**：若 TRACECLK 有活动但采集 MMCM 持续 ~134ms 未锁,自动脉冲一次软复位尝试重锁（self-healing）。针对 MMCM 上电偶发不锁 / 相位边界抖动。

> 这直接解决了"出问题方便恢复"的诉求——大多数卡死不再需要 openFPGALoader 重烧或物理断电。

### 3.4 "一键体检"主机脚本

配套一个 `fpga_health.py`,读上面寄存器堆,输出人话诊断：
```
$ python3 fpga_health.py
[OK]   debug module online (magic=0xDB v1)
[OK]   TRACECLK active, MMCM locked
[OK]   capture FIFO occ=12/1024 (max=340)
[FAIL] self-TX FSM: FIRST_ERR=0x401 (HDR stuck / ARP not resolved) @ t=1.23s
       FSM_STATE: self_tx=HDR(1)  -> stuck 8.0ms, entered BACKOFF x37
       => 根因:self-TX 等 ARP 解析,主机未回 ARP / ARP 缓存空
诊断结论:网络自发 TX 死锁(HANDOFF §7.1)。建议:主机回 ARP,或修 FSM。
```
**这就是把"十几轮猜谜"压缩成一条命令。**

---

## 4. 分期实施建议（按性价比）

| 阶段 | 内容 | 成本 | 收益 |
|---|---|---|---|
| **P1（先做）** | ① 把现有被丢弃的 error 端口全接进一个 `dbg_regfile`;② 暴露 self-TX FSM 的 `st`/`hdr_timeout`/`stuck`;③ sticky 首错码+时间戳;④ 复用 :5001 加一个 :5003 debug 读出页;⑤ `fpga_health.py` | 小（~几百 LUT,纯逻辑,不动数据通路） | **立刻**解决"0 包不知道哪坏"的头号痛点 |
| **P2** | ⑥ 启用板载 UART 控制台（读 regfile + 简单命令）,做成**网络无关**的后备通道;⑦ FSM 看门狗自动报错码 | 小-中 | 网络挂了还能诊断（这次就需要） |
| **P3（按需）** | ⑧ 在关键模块预留 ILA 挂载点（signal mark_debug）,需要时开一个综合开关插 ILA 抓波形 | 中（重综合） | 信号级时序问题的二线手段 |

> 注意 35T 资源紧（proposal 10）。P1 纯是把已有信号"接出来 + 锁存",增量极小;ILA 才吃 BRAM,放 P3 按需。

---

## 5. 这套设计如何直接治好当前的病

回到 self-TX 死锁这个具体案例,有了 P1 后：
1. 抓到 0 包 → 跑 `fpga_health.py` → 立刻看到 `FIRST_ERR=0x401 self-TX HDR stuck`,**3 秒定位**,不用 halt/resume 反复试。
2. `FSM_STATE` 显示 self-TX 卡在 HDR + BACKOFF 次数,直接印证"ARP 未解析"根因。
3. 修复后,同一脚本可回归验证（错误码不再出现 = 修好了）。

而且这套机制是**通用**的——以后 ETM 溢出（proposal 29）、去帧错、采样失锁,都能被对应错误码即时捕获,不必每次从零 debug。

---

## 6. 结论

当前最大的调试痛点是 **FPGA 内部不可观测**:关键 FSM 状态、FIFO 水位、错误信号要么在 RTL 里出不来,要么直接被 `.port()` 空接丢弃,导致"没 trace"这一个现象背后 7 个可能环节无法区分,只能靠外部现象反复猜。

业内的答案是**分层可观测性**:一线是**常驻、数据通路无关的状态/错误码寄存器堆 + 独立读出通道 + sticky 首错锁存**,二线才是 ILA/VIO。本提案的 P1（把丢弃的错误信号接出来 + 暴露 self-TX FSM + 首错码 + `fpga_health.py`）成本极低却能立刻把调试从"小时级猜谜"降到"秒级定位",强烈建议先做——它会让后续所有上板工作（CPU 降速验证、满速 PoC、LVGL）都受益。
