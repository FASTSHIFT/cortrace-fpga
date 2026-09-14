# 24 — 数据源隔离：污染在采样前端，不在 DDR-ring/UDP 通路

日期：2026-09-07
被测 bitstream：`trace_ddr_stream.bit`（BUILD_ID 0x6a9aa5e7，tap2/IDELAY 版），
当时板上在跑。本测试与前端配置无关——selftest CSR 完全旁路了 trace 引脚。

## 问题

之前的工作（doc 23 复盘、TASK 7 记录）在解码出的 ETM 流里看到周期性污染：
反复出现的 `0x96`、gap=3 聚集、几 MB 的"真实"采集里只能捞出个位数的 ETMv4
A-sync。悬而未决的问题是：这个污染到底是谁注入的：

* **采样前端**（trace_capture_a7 采物理 TRACECLK / TRACEDATA 引脚——SI/眼图问题），
  还是
* **数据通路**（la_ddr_writer 打包 → DDR3 ring → la_ddr_ring_streamer gearbox
  → packetiser → UDP——逻辑/CDC 问题）。

这两者之前一直被混在一起。本测试就是把它们分开的单变量实验。

## 方法

`trace_ddr_stream_top` 有两个 FPGA 内部字节源，喂给与真实 trace **完全相同**的
DDR-ring → UDP 通路，运行时切换、无需重烧：

| CSR | 值 | 数据源 |
|-----|-----|--------|
| 0x09 | 1 | 自由跑的 **ramp**（byte = ++计数器） |
| 0x09=1, 0x0B=1 | | **固定 0x42** |
| 0x09=0 | | 真实 trace（采样引脚） |

两个内部源都在 clk200 域的 `src_byte` 处注入，位于 la_ddr_writer **之前**，所以
它们走的是与真实 trace 完全一致的 打包/DDR3/gearbox/packetiser 逻辑。它们表现出
的任何污染 100% 是数据通路问题；只在真实 trace 出现、内部源不出现的污染 100% 是
前端问题。

采集工具：`stream_grab`（C，零丢包，recv 与磁盘写解耦）。每次 3 秒，约 335 MB，
走 `enxc8a36266dcae`。

## 结果

| 数据源 | 字节数 | 污染 | 细节 |
|--------|--------|------|------|
| 固定 0x42 | 335,373,312 | **0** 个非-0x42 | 主机端 + FPGA diag latch（194k 包 / 0 坏） |
| ramp | 335,373,312 | **0** 个 ramp 断点 | 每个 `a[i+1] == a[i]+1 (mod 256)`，跨所有包边界 |
| 真实 trace | 335,373,312 | 严重 | 249 个不同字节值；0x96 在 **gap=1024**（每 payload 一次）复现 |

固定和 ramp 流在**各自** 335 MB 上逐字节完美。ramp 是两者中更强的证据：它是时变
数据，跨越了每个 packet 边界、每个 DDR burst 边界（LENGTH=64 words）、以及
128b→8b 的 gearbox，却保持单调、零断点。如果 writer 打包、DDR ring 地址运算、
streamer gearbox、或 CDC FIFO 丢/重/乱了哪怕一个字节，ramp 就会在那里出现断点。
它没有。

## 结论

**FPGA→DDR-ring→UDP 数据通路可证明是干净的。** 打包（大端 16 字节 word）、DDR3
ring（+8 app-addr/word、wrap）、writer/streamer 并发共用 arbiter、161-bit
{rtx,seq,data} CDC FIFO、clk125 gearbox、packetiser，在 112 MB/s 持续速率下，对
常量和时变 payload 都零错。

**污染完全来自物理引脚采样前端。** 这与 doc 23 的 SI/眼闭故事一致：当时板上跑的
是 tap2/IDELAY 版，STM32（按已提交固件）boot 进 selftrace 循环。`0x96`-在-gap-1024
的特征是与 packet payload 长度对齐的采集伪象，不是传输 bug——传输把 ramp 和 fixed
跨过那些同样的边界都完好搬运了。

### 这退役了哪些假设

* "la_ddr_writer 打包的周期-3 污染" —— **证伪**。ramp 穿过打包器保持单调。
* "DDR-ring / gearbox / CDC 丢字节" —— **证伪**。合计 670 MB 零丢零乱序。
* 再查数据通路都是白费功夫。杠杆在前端 SI：降 trace 引脚频率（R=4 → 56MHz 引脚，
  doc 23 眼张开）和/或改板级 SI，正如 doc 23 的结论。

### 复现

```
# 固定 0x42
python3 -c "...csr write 0x09=1, 0x0B=1..."
sudo ./stream_grab enxc8a36266dcae 3 captures/fixed42.bin
python3 read_bad_byte_latch.py --iface enxc8a36266dcae   # 期望 bad_count=0

# ramp
python3 trace_ctrl.py stream-selftest 1                   # 0x09=1, 0x0B=0
sudo ./stream_grab enxc8a36266dcae 3 captures/ramp.bin
# 分析：每个字节 delta == 1 mod 256

# 真实
python3 trace_ctrl.py stream-selftest 0 ; python3 trace_ctrl.py rearm
sudo ./stream_grab enxc8a36266dcae 3 captures/real.bin
```
