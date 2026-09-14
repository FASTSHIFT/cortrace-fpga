# 2-bit 满速 LVGL 函数级 Trace → Perfetto 可视化：差距分析与工作拆解

> **目标**：在 STM32F429 上用 **2-bit DDR 并行 trace**、**CPU 满速 168 MHz**、
> **不开 stall**，抓取 LVGL 固件的完整函数调用流，经 orbetto 解码后生成
> **Perfetto 函数级 timeline**（嵌套调用栈 + 真实墙钟时间轴）。
>
> **本文档拆解"离这个目标还有多远"，给出可执行的工作清单。**

---

## 0. 一句话结论

**采集侧（FPGA）已基本就绪，解码侧（orbetto 调用栈）是主要瓶颈，满速带宽是次要风险。**

当前已在 **21 MHz TRACECLK（HCLK/4 = 42 MHz 降频）** 下跑通 LVGL 流式 trace，
得到 65040 个命名调用栈 slice、98% 命名率。要推到 **84 MHz TRACECLK（HCLK/1 = 168 MHz 满速）**，
需要解决三个层面的问题：

| 层面 | 当前状态 | 满速目标 | 差距 |
|------|---------|---------|------|
| **FPGA 采集** | 21M golden，84M 数据采到但原生组装错 | 84M 原生 golden | RTL 字节组装 bug |
| **带宽匹配** | 降频下 2-bit DDR 够用 | 满速 2-bit DDR vs ETM 码率 | 需实测验证 |
| **解码调用栈** | 98% 命名，间接返回后偶尔脱轨 | 100% 连续嵌套 | orbetto return-stack 收敛 |

---

## 1. 当前已验证的基线

### 1.1 已跑通的端到端链路

```
STM32F429 ETM (4-bit, br_out=0, stall ON)
  → trace_capture_mmcm (MMCM 90° 相移, IDDR)
  → axis_async_fifo (clk90 → clk125)
  → 打包器 [4B seq][payload] → fpga_core_net self-TX
  → 千兆 UDP :5555 → trace_stream_rx.py
  → mmcm_decode.py (nibble 恢复 + parity 搜索 + TPIU deframe)
  → mmcm_stream_orbetto.py (uniform 时基 → .tpiu + .fpga_ns)
  → orbetto -t 2 -f <tpiu> -e <elf> -F <fpga_ns>
  → Perfetto JSON (mmcm_stream_timeline.py)
```

### 1.2 关键实测数据（降频 21M 路径）

| 指标 | 实测值 | 来源 |
|------|--------|------|
| TRACECLK | 21 MHz (HCLK/4) | proposal 22 §7 |
| 吞吐 | 20.9–21.1 MB/s | commit 910e531 |
| UDP 丢包 | 0（稳态） | commit 910e531 |
| ETM unknown 率 | 0.0022%（启动后窗口） | commit 910e531 |
| 重建指令数 | 2,042,512 | commit 910e531 |
| LVGL capture | 4 MB / 190 ms | PERF_FINDINGS.md |
| LVGL 调用栈 slice | 65040（98% 命名） | commit b69737c |
| LVGL anchor 函数 | 203 个真实函数 | PERF_FINDINGS.md |

### 1.3 已修复的关键 bug

| Bug | 修复 | 提交 |
|-----|------|------|
| UDP self-TX 零包 | `UDP_CHECKSUM_GEN_ENABLE=0` | 0f19a41 |
| orbetto deframe 过滤错误 | `tpiu_deframe_walk` 连续重对齐 | b69737c |
| loadelf symtab-only 函数 NULL | `_addSymtabFunctions()` fallback | b03286b (orbuculum) |
| orbetto 间接分支后猜地址 | 间接分支后停等 Branch Address 包 | 6e942af |

---

## 2. 满速 84M 的三个差距

### 2.1 差距 A：FPGA 84M 原生字节组装 bug（采集侧）

**现状**（proposal 22 §7.4）：

| TRACECLK | 原生 HSYNC | 原生 unknown | 原生锚点 | PC 端重搜后 |
|----------|-----------|-------------|---------|------------|
| 21M | ✅ 多 | 0.01% | 36 | ✅ golden |
| 42M | ❌ 0 | 17% | 1 | ✅ 17 锚点 |
| **84M** | **❌ 0** | **21.9%** | **0** | ✅ 18 锚点（135° 相位） |

**根因**：`trace_capture_mmcm.v` 的字节组装逻辑（`{trace_a[k], trace_b[k-1]}`）
在 21M 下正确，但 42M/84M 下半位配对错位。当前靠 PC 端 `mmcm_decode.py` 的
parity/order 搜索离线救回，但 RTL 原生输出是坏的。

**影响**：流式模式下 PC 端离线重搜可以工作（当前 LVGL trace 就是这么做的），
但原生组装正确才能简化解码管线、降低 unknown 率。

**工作量**：中。需要理解 `trace_capture_mmcm.v` 的 `{trace_a, trace_b}` 配对逻辑
在不同频率下的半位偏移，在 RTL 里做频率自适应或固定 84M 配对。

### 2.2 差距 B：满速 2-bit DDR 带宽 vs ETM 码率（带宽侧）

**2-bit DDR 带宽**（proposal 21 §3.1）：

| TRACECLK | 2-bit DDR 带宽 |
|----------|---------------|
| 21 MHz | 84 Mbit/s = 10.5 MB/s |
| **84 MHz** | **336 Mbit/s = 42 MB/s** |

**ETM 码率估算**（br_out=0，proposal 21 §3.2）：

| CPU 频率 | ETM 码率（估） | vs 2-bit DDR @84M |
|----------|---------------|-------------------|
| 42 MHz (当前) | ~33 Mbit/s | ✅ 余量 10× |
| **168 MHz (满速)** | **~131 Mbit/s** | ✅ 余量 2.6× |

**关键不确定性**：§3.2 的 0.78 bit/CPU 周期是 proj_add 量级估算，LVGL 的间接调用
密集代码码率可能更高。需要实测确认。

**影响**：如果 LVGL 实际码率 > 336 Mbit/s，FIFO 会溢出丢数据。但 2.6× 余量
大概率够用——LVGL 不是纯循环，函数调用占比 11.8%（文档 1 §4.3.1），间接分支
地址包才是码率大头。

**工作量**：低。主要是跑一次满速捕获，看 `lost_cnt` 和 unknown 率。

### 2.3 差距 C：orbetto 间接返回调用栈脱轨（解码侧）

**现状**（doc 14 §11，PERF_FINDINGS.md）：

- orbetto 已改为间接分支后停等 Branch Address 包（6e942af）
- proj_add（直接调用为主）：4438 slice，100% 命名，完美嵌套
- LVGL（间接调用密集）：65040 slice，98% 命名，**残留 2% raw + 稀疏脱轨点**

**根因**（doc 14 §11.2）：间接返回（`pop {pc}` / `bx lr`）后，orbetto 等下一个
Branch Address 包重新锚定。但如果两个锚点之间有噪声/丢字节，解码器会跑飞直到
下一个 I-sync。

**影响**：这是从"98% 可用"到"100% 完美"的最后一公里。当前 LVGL timeline 已经
能看出函数热点（lv_timer_handler 8.5%、lv_draw_sw_line 7.2% 等），但嵌套调用栈
在间接返回处有断点。

**工作量**：中高。需要对齐 orbmortem 的 `stackDelPending` 语义，处理递归/中断
导致的栈失衡。这是上游解码器 territory（doc 14 §11.4）。

---

## 3. 工作拆解

### Phase 1：满速 2-bit 链路验证（1-2 天）

> **目标**：在 84M TRACECLK 满速下，2-bit DDR 采集零丢包、解码 golden。

| # | 任务 | 依赖 | 产出 |
|---|------|------|------|
| 1.1 | 修改 `etm_2bit.cfg`：stall OFF（`ETMCR = 0x080`） | 无 | 不阻塞 CPU 的 2-bit 配置 |
| 1.2 | 构建 84M 2-bit 流式 bitstream：`WIDTH=2 MULT=10 DIVID=10 PHASE=112.5` | 1.1 | `trace_mmcm_stream_2bit_84m.bit` |
| 1.3 | 满速捕获 LVGL，检查 `lost_cnt` 和 UDP 序列号 | 1.2 | 带宽余量实测数据 |
| 1.4 | PC 端解码（parity 搜索），检查 unknown 率和锚点数 | 1.3 | 满速 vs 21M 质量对比 |
| 1.5 | 如果 unknown > 5%：扫相位（90°–135°），找 84M 2-bit 最优相位 | 1.4 | 84M 2-bit golden 相位 |

**关键文件**：
- `target/etm_2bit.cfg` — 改 stall OFF
- `fpga_flow/run_trace_mmcm_stream.tcl` — 加 `WIDTH=2` generic 传递
- `scripts/trace_stream_rx.py` — 收流
- `decode/mmcm_decode.py` — 解码

**风险**：84M 2-bit 的 SI 可能比 4-bit 好（线少一半），但相位窗口可能更窄。
如果固定相位不够稳，需要 Phase 4 的动态相位校准。

### Phase 2：orbetto 调用栈收敛（3-5 天）

> **目标**：LVGL 间接返回后零脱轨，100% 连续嵌套调用栈。

| # | 任务 | 依赖 | 产出 |
|---|------|------|------|
| 2.1 | 复现脱轨点：用 Phase 1 的满速 LVGL capture，定位所有 raw PC slice | 1.4 | 脱轨点列表（PC + 前后指令） |
| 2.2 | 分析脱轨根因：是噪声丢字节、还是栈候选过期、还是递归/中断栈失衡 | 2.1 | 根因分类 |
| 2.3 | 修复 orbetto `traceDecoder.c`：对齐 `stackDelPending` 语义 | 2.2 | orbetto patch |
| 2.4 | 处理中断导致的栈失衡：中断返回时栈帧不匹配的恢复策略 | 2.3 | orbetto patch |
| 2.5 | 回归测试：proj_add + LVGL，slice 命名率 → 100% | 2.4 | 验证报告 |

**关键文件**：
- `embedded-debug-tools/ext/orbetto/src/traceDecoder.c` — 调用栈重建逻辑
- `embedded-debug-tools/ext/orbetto/src/Mortrall.c` — return-stack 管理
- `decode/PERF_FINDINGS.md` — 质量记录

**参考**：doc 14 §11.3 已有修复方向（间接分支后停等地址包），§11.4 给出了
对齐 `stackDelPending` 的路径。

### Phase 3：Perfetto 可视化打磨（1-2 天）

> **目标**：生成可直接拖入 ui.perfetto.dev 的函数级 timeline。

| # | 任务 | 依赖 | 产出 |
|---|------|------|------|
| 3.1 | 完善 `mmcm_stream_timeline.py`：嵌套调用栈 + 函数耗时 | 2.5 | Perfetto JSON（嵌套 track） |
| 3.2 | 加函数耗时统计：每个函数的 wall-clock 占比、调用次数 | 3.1 | 函数热点表 |
| 3.3 | 加 CPU 占用率 track：基于 I-sync 频率的 CPU 活跃度 | 3.1 | Perfetto counter track |
| 3.4 | 一键脚本：`lvgl_trace.sh`（ETM → FPGA → 收流 → 解码 → Perfetto） | 3.2 | 端到端脚本 |

**关键文件**：
- `decode/mmcm_stream_timeline.py` — Perfetto JSON 生成
- `decode/etm_to_perfetto.py` — Perfetto 事件构建
- `scripts/lvgl_trace.sh` — 一键脚本（新建）

### Phase 4：稳定性加固（2-3 天，可选）

> **目标**：一个 bitstream 通吃全频段，无需逐频手扫相位。

| # | 任务 | 依赖 | 产出 |
|---|------|------|------|
| 4.1 | RTL：`MMCME2_BASE` → `MMCME2_ADV`，加 PSEN/PSINCDEC/PSDONE | 无 | 动态相位移位 RTL |
| 4.2 | CSR：主机可触发相位扫描，读 unknown 率挑最低点锁定 | 4.1 | 相位校准状态机 |
| 4.3 | 启动 ARP FIFO 丢弃修复：ARP 完成前不计数 | 无 | 零 artifact 启动 |
| 4.4 | self-TX FSM 架构清理：切换到独立 `udp_tx_streamer.v` | 无 | core 回归纯以太网核 |

---

## 4. 依赖关系与关键路径

```
Phase 1 (满速链路) ──────┐
                         ├──→ Phase 3 (Perfetto 可视化)
Phase 2 (调用栈收敛) ────┘
                         │
Phase 4 (稳定性加固) ────┘ (可选，与 Phase 2/3 并行)
```

**关键路径**：Phase 1 → Phase 2 → Phase 3（串行依赖）

**总工期估算**：
- 乐观（一切顺利）：5-7 天
- 典型（有 1-2 个坑）：8-12 天
- 保守（84M SI 问题 + 调用栈深坑）：2-3 周

---

## 5. 风险与缓解

| 风险 | 概率 | 影响 | 缓解 |
|------|------|------|------|
| 84M 2-bit SI 不够（眼图闭合） | 中 | 需降频或加 IDELAY | 先试 42M（HCLK/2），SI 更友好 |
| LVGL 满速码率超 336 Mbit/s | 低 | FIFO 溢出丢数据 | 开 stall 或升 4-bit |
| orbetto 调用栈修复涉及上游重构 | 中 | 工期延长 | 先用 anchor-level timeline 交付 |
| 84M 相位窗口太窄 | 中 | 随温度漂移 | Phase 4 动态相位校准 |
| 中断导致栈失衡难修复 | 中 | 残留少量脱轨 | 容忍 <1% 脱轨，标注诚实边界 |

---

## 6. 交付物定义

### 6.1 最小可行交付（MVP）

- 84M 2-bit 满速 LVGL capture（零稳态丢包）
- Perfetto JSON：anchor-level flat timeline（函数热点，已可做）
- 函数热点表：top-20 函数 wall-clock 占比

### 6.2 完整交付

- 84M 2-bit 满速 LVGL capture
- Perfetto JSON：**嵌套调用栈**（100% 命名，间接返回不断）
- 函数耗时统计 + CPU 占用率 track
- 一键脚本 `lvgl_trace.sh`
- 质量报告：slice 数、命名率、unknown 率、丢包率

### 6.3 诚实边界（必须在交付物中标注）

- anchor-level timeline 的 flat slice 时长是粗粒度归属（跨到下一个 I-sync）
- 如果 orbetto 调用栈未 100% 收敛，标注残留脱轨率
- 84M 相位是固定值，未做动态校准时标注"随板/温度可能漂移"

---

## 7. 立即可执行的第一步

```bash
# 1. 改 etm_2bit.cfg: stall OFF (ETMCR 0x880 → 0x080)
# 2. 构建 84M 2-bit 流式 bitstream
cd syn/artix7/bringup
WIDTH=2 MULT=10 DIVID=10 TRACE_PERIOD=11.9 PHASE=112.5 \
  vivado -mode batch -source fpga_flow/run_trace_mmcm_stream.tcl

# 3. 使能 2-bit ETM (stall off)
openocd -f interface/stlink.cfg -f target/stm32f4x.cfg -f target/etm_2bit.cfg

# 4. 烧录 FPGA
scripts/program.sh mmcm_stream jtag

# 5. 收流
python3 scripts/trace_stream_rx.py --ip 192.168.10.42 -o /tmp/lvgl_84m.bin

# 6. 解码 + Perfetto
cd decode
python3 mmcm_stream_orbetto.py /tmp/lvgl_84m.bin lvgl_84m --period-ns 11.9
python3 mmcm_stream_timeline.py /tmp/lvgl_84m.bin /tmp/axf/proj.axf lvgl_84m --period-ns 11.9
# → lvgl_84m.json → 拖入 https://ui.perfetto.dev
```

---

## 附录 A：2-bit vs 4-bit 满速对比

| 指标 | 2-bit DDR @84M | 4-bit DDR @84M |
|------|---------------|----------------|
| 带宽 | 336 Mbit/s (42 MB/s) | 672 Mbit/s (84 MB/s) |
| 引脚 | 3 (CLK+D0+D1) | 5 (CLK+D0-D3) |
| SI 难度 | 中（2 根等长数据线） | 高（4 根等长 + nARMED 命门） |
| vs LVGL 码率 | 余量 2.6× | 余量 5.2× |
| 当前验证 | 21M golden，84M 待验 | 21M/84M 均已验证 |

2-bit 的 SI 优势（线少一半、无 nARMED 长线命门）使其在满速下比 4-bit 更容易
拿到稳定眼图。带宽余量 2.6× 对 LVGL 函数级 trace（br_out=0）大概率够用。

## 附录 B：ETM 码率估算依据

| 参数 | 值 | 来源 |
|------|-----|------|
| 编码密度 | 1.12 bit/指令 | sidetrack §13.2 实测 |
| IPC | ~0.7 | 估算 |
| bit/CPU 周期 | ~0.78 | 1.12 × 0.7 |
| LVGL 函数调用占比 | 11.8% | 文档 1 §4.3.1（vela_ap.elf） |
| br_out=0 地址包占比 | ~10% | proposal 19 §1 实测 |
| P-header 占比 | ~89% | proposal 19 §1 实测 |

> ⚠️ 0.78 bit/CPU 周期是 proj_add 量级估算，LVGL 间接调用密集代码可能更高。
> Phase 1.3 的实测将给出真实码率。
