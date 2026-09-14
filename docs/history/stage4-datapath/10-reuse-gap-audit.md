# Stage-4 · 复用缺口审计:网口前的逻辑哪些该复用 orbtrace 而没复用

> 原则(用户明确):**从网口出来之前的那段逻辑,应尽量复用 orbtrace,而不是重写。** 本文逐模块审计现状,标出"该复用没复用"的项和补救方式。
>
> 依据:`orbtrace/orbtrace/trace/*.py`(gateware 权威)、`export_trace_modules.py`(我们导出了什么)、`syn/artix7/bringup/*.v`(我们写了什么)。

---

## 0. 结论速览

| 逻辑块 | orbtrace 有 | 我们现状 | 复用情况 |
|--------|------------|----------|----------|
| traceIF(采样组帧) | `verilog/traceIF.v`(原生 Verilog) | 直接 `read_verilog` 同一文件 | ✅ 已复用 |
| TPIUDemux | `trace/tpiu.py` | 导出成 `tpiu_demux.v` 例化 | ✅ 已复用(导出) |
| ChecksumAppender | `trace/orbflow.py` | 导出 `checksum_appender.v` | ✅ 已复用(导出) |
| COBSEncoder | `trace/cobs.py` | 导出 `cobs_encoder.v` | ✅ 已复用(导出) |
| SuperFramer | `trace/orbflow.py` | 导出 `super_framer.v` | ✅ 已复用(导出) |
| **TPIUSync** | `trace/tpiu.py` | **没导出、没用** | ❌ **该复用没复用** |
| **TraceIF amaranth 包装** | `trace/core.py` `TraceIF` | 自己在 V 里接 traceIF | ⚠️ 半复用(逻辑等价,接法自造) |
| **trace_fifo CDC + Monitor/lost** | `core.py` + `util.py` | 自己写 `axis_async_fifo` + `trace_lost_cnt` | ⚠️ 重写(功能等价) |
| **整条 TraceCore 串接** | `core.py` `TraceCore` | 自己写 `trace_orbflow_top.v` 串 | ❌ **该整体导出没导出** |
| 出口(USB/网口) | `amaranth_glue/luna.py` USB | 千兆 UDP(平台不同) | N/A(平台决定,合理不同) |

**核心缺口两个**:
1. **TPIUSync 没复用** —— 它正是"找 `0xFFFFFF7F` 全同步 + 过滤 `0x7FFF` 半同步 + 字节重对齐"的模块,**恰好是我们卡了好几轮的字节对齐问题的现成答案**。
2. **没有把整条 `TraceCore` 作为一个单元导出**,而是手工在 Verilog 里重新串了 4 个导出模块 + 自造 CDC/glue,导致接法、format 选择(trace path vs bypass)这些容易接错的地方都是我们自己重走一遍。

---

## 1. 关键缺口①:TPIUSync —— 字节对齐的现成答案,我们没用

`trace/tpiu.py` 的 `TPIUSync`:

```python
with m.If(Cat(self.input.payload, buf)[:32] == 0xffffff7f):   # 全同步 -> 锁定
    synced.eq(1); buf.eq(1)
with m.Elif(Cat(self.input.payload, buf)[:16] == 0xff7f):     # 半同步 -> 丢弃
    buf.eq(buf[8:])
with m.Else():
    buf.eq(Cat(self.input.payload, buf))                       # 累积成 128-bit 帧
```

- 这模块吃**逐字节流**,自己找 `0xFFFFFF7F` 锁帧、滚动累积出 16 字节 TPIU 帧、过滤 `0x7FFF` 半同步 —— **天然处理字节对齐**(逐字节滚动直到同步字出现)。
- core.py 里它只在 SWO 路径(0x11/0x13)用;**但它本质是"把任意字节流对齐成 TPIU 帧"的通用件**。
- **我们的链路恰恰缺这个**:traceIF 用私有方式组帧,和下游 TPIUDemux 期望的对齐对不上,我们前几轮一直在 PC 端手搓字节对齐 —— 而 orbtrace 早有 TPIUSync。

**补救**:把 TPIUSync 也加进 `export_trace_modules.py` 导出成 `tpiu_sync.v`,在数据通路里插到 traceIF/原始字节流 → TPIUDemux 之间(或直接喂原始采样字节给 TPIUSync,跳过 traceIF 的私有组帧)。

## 2. 关键缺口②:没有整体导出 TraceCore

`core.py` 的 `TraceCore` 已经把 `traceif → trace_fifo(CDC) → tpiu_demux → checksum → cobs → superframer → fifo` 全部正确串好,还带:
- `input_format` 选择(trace 0x01-0x03 走 demux,SWO 走 bypass/sync)
- `util.Monitor` 的 lost/total/clk 计数(溢出可观测)
- 正确的 CDC(AsyncFIFOBuffered)

我们却在 `trace_orbflow_top.v` 里**手工重串**了 4 个导出模块 + 自写 `axis_async_fifo` CDC + 自写 `trace_lost_cnt`。功能等价,但:
- 接法是我们自己重走(字节序、valid/ready、format 分支都可能接错,实际也踩过)。
- Monitor/lost 的语义和 orbtrace 不一致,PC 端工具对不上。

**补救**:用 amaranth 把整个 `TraceCore`(DomainRenamer 处理好 trace/sync 时钟域)导出成**一个** `trace_core.v`,FPGA 顶层只接 `trace_a/trace_b/trace_clk` 输入和 `output`(OrbFlow 字节流)输出 + `input_format`。这样网口前的逻辑**100% 是 orbtrace 的**,我们只接 I/O。

## 3. 半复用项(可接受但值得统一)

- **TraceIF amaranth 包装**(core.py):它把 `verilog/traceIF.v` 包了一层,负责 `width`、`FrAvail→valid` 的 stream 化。我们直接接的裸 traceIF.v,逻辑等价但少了 stream 语义。若整体导出 TraceCore,这层自然包含,统一掉。
- **CDC**:我们的 `axis_async_fifo`(verilog-ethernet)和 orbtrace 的 `AsyncFIFOBuffered`(amaranth)功能等价;整体导出后用 orbtrace 的,省得两套。

## 4. 合理的不同(不需要复用)

- **出口传输**:orbtrace 是 LUNA USB2,我们是千兆 UDP —— **平台决定,合理不同**(A7-Lite 无 USB2 PHY)。网口本身的 MAC/UDP 用 verilog-ethernet,也是成熟复用。
- **trace_capture_a7**(IDELAY/IDDR 采样前端):Artix-7 特有原语,ECP5 的 orbtrace 没有对应件,**必须自己写**,合理。

---

## 5. 行动项(按价值)

1. **导出 TPIUSync → `tpiu_sync.v`**,插入数据通路解决字节对齐(直接对应我们卡的问题)。先验证:原始采样字节流 → TPIUSync → TPIUDemux 是否收敛到单一 ETM 通道。
2. **整体导出 `TraceCore` → `trace_core.v`**,FPGA 顶层只接 I/O,网口前逻辑全用 orbtrace 的,删掉 `trace_orbflow_top` 里手工重串的部分。
3. 保留我们必须自写的:`trace_capture_a7`(A7 采样)、网口出口(平台)。

> 一句话:**4 个管线模块已复用,但漏了 TPIUSync(恰是字节对齐的答案),且没有整体导出 TraceCore 而是手工重串。补这两项,网口前就基本 100% 是 orbtrace 的逻辑,我们只负责 A7 采样前端和网口出口。**


---

## 6. 行动①执行结果(实测,2026-06-13)

按行动项导出了 `TPIUSync`(`export_trace_modules.py` 新增 `_wrap_tpiu_sync` → `tpiu_sync.v`,iverilog elaboration 通过),并把它的逻辑**忠实移植成 Python 参考模型** `etm35lib.tpiu_sync_frames`(单测覆盖 + 真实抓取验证)。两个硬结论:

1. **formatter 帧确实存在**:把原始 nibble 采样(`/tmp/trace_etm.bin`)喂 orbtrace 的 TPIUSync 逻辑,**装出 820 个对齐的 16 字节 TPIU 帧**。证明 STM32 TPIU formatter 一直在工作、引脚上就是 formatter 帧 —— 推翻了之前"裸 ETM 无帧"的判断。

2. **trace_a/trace_b nibble 接反了**(新发现的真 bug):
   - nibble 交换后(`{a<<4 | b}`)→ **820 帧**
   - 不交换 → **0 帧**
   - 说明我们 `trace_capture_a7`/`traceIF` 喂入的 trace_a(上升沿)和 trace_b(下降沿)两个 nibble **高低位接反**。这正是为什么之前 traceIF 组帧后下游 demux 散乱、PC 端怎么对齐都不收敛。

### 顺带纠正一个错误假设
`decode_aligned` 当初假设"I-sync 紧跟在 A-sync 后的小窗口内"。但 IHI0014Q §7.10.3 明确:**周期 A-sync 和周期 I-sync 由独立计数器驱动,不必相邻**。实测 fixture 里 I-sync 在 A-sync 之后 ~1000 字节。所以解码必须**独立锚定 I-sync**(`find_isyncs`),不能靠"A-sync 后找 I-sync"的窗口。

### 修正后的正确路线(收敛)
既然 formatter 帧是真的、只是 nibble 接反:
1. **改 `trace_capture_a7`/`traceIF` 接线:交换 trace_a/trace_b nibble**(或在 traceIF 输入处 swap),让 FPGA 直接产出正确字节序的流。
2. FPGA 侧用导出的 `tpiu_sync.v`(orbtrace 原生)做字节对齐成 16 字节帧 → 喂 `tpiu_demux.v` → checksum → cobs → super_framer。**网口前整条链全是 orbtrace 的逻辑。**
3. 这样 PC 端 orbcat/orbmortem 原生解,不用任何字节对齐 hack。

> 行动①的最大收获不是"导出了 TPIUSync",而是用它**坐实了 nibble 接反这个真 bug** —— 这比子字节重对齐更根本,改一处接线就对。


---

## 7. 离线复算的边界 + 落地 SWAP_NIBBLES 参数(诚实记录)

把 nibble-swap 后的原始流跑完整 TPIUSync→Unmangle→TrackStream 链:
- TPIUSync 锁出 **820 帧**,但 Unmangle 后通道仍散到 ~40 个(ch58 占 40%,未收敛到单一)。
- 复查 `0xFFFFFF7F` 间距:**不规则**(4/108/258/252/556...),不是周期 formatter 同步。间距=4 是 `ff 7f ff 7f` 半同步 idle 被误当全同步。
- 帧内容大量 `75ff`/`75ffffff` —— TPIU 半同步 idle(`0x7FFF`)的 unmangle 残影。

**诚实判断**:这份老抓取(`/tmp/trace_etm.bin`,16KB,共地前、旧顶层、且 STM32 当时多在 idle)**信息量不足以离线定论 framing**——它大部分是 TPIU 半同步 idle。nibble-swap 让"能不能锁帧"从 0→820 是真信号(说明 nibble 确实接反了一层),但"通道是否收敛"需要一份**真实繁忙、且 nibble 修正后**的新抓取才能判。

**落地动作(低风险、为下次抓取铺路)**:给 `trace_orbflow_top` 加 `SWAP_NIBBLES` 参数(build 时 `SWAP_NIBBLES=1` 环境变量切换),把 traceIF 的 traceDina/traceDinb 互换。iverilog elaboration 通过。下次上板:
1. `SWAP_NIBBLES=1 ./build.sh orbflow` 重新综合;
2. 先配 ETM(让 STM32 跑繁忙负载)→ re-arm → 抓;
3. 对比 swap=0/1 两版的 TPIU 帧收敛度,定论 nibble 接线。

> 不硬下"nibble 接反就是全部根因"的结论——离线老数据只够证明 swap 影响锁帧,收敛性要新抓取验。参数已就位,下次上板一次测两版。


---

## 8. 上板 A/B 实测:SWAP_NIBBLES + OrbFlow 路径(决定性)

综合了 `SWAP_NIBBLES=1` 的 OrbFlow 顶层(0 error,时序收敛),上板抓取对比:

| 抓取 | 路径 | I-sync 锚点 | 结论 |
|------|------|------------|------|
| `capGND.bin`(trace_stream,swap=0) | traceIF 帧,**不过 tpiu_demux** | **14** | 能解出真 PC |
| `capDIV16.bin`(trace_stream,swap=0) | 同上 | **15** | 能解出真 PC |
| `oflow_swap.bin`(OrbFlow,swap=1) | traceIF→**tpiu_demux**→cobs | **0** | 全毁 |

OrbFlow(swap=1)的 OFLOW 解出:tag 127 占 70%(全 1 通道,idle 残影),**重组后 0 个 I-sync** —— tpiu_demux 把可解的 ETM 数据**搅毁了**。

### 两个决定性结论
1. **nibble swap 不是 OrbFlow 路径的解**:swap=1 让 OFLOW 更糟(0 锚点 vs swap=0 路径的 14)。之前"820 帧只在 swap 序"是**原始 nibble 喂 TPIUSync**那条路的现象,和 OrbFlow 顶层(traceIF→tpiu_demux)是两条不同的路,不能混。
2. **tpiu_demux 对我们这股流是错的组件**:`trace_stream`(只到 traceIF 帧、不过 demux)稳定 14-15 个 I-sync 锚点能解出真函数;一旦过 tpiu_demux 就归零。坐实:**我们的流不是 demux 期望的"对齐 TPIU formatter 帧",硬过 demux 只会毁数据。**

### 修正路线(由消去法确定)
**放弃"FPGA 侧串 tpiu_sync→tpiu_demux 产 OFLOW"这条对我们流不成立的路。** 正确做法:
- FPGA 侧只到 **traceIF 帧**(`trace_stream_top`,已验证 14-15 锚点),把 traceIF 字节流直接经网口送 PC;
- PC 端用 **`etm35lib` 直接锚定 I-sync**(已 100% 测试覆盖、能解真 PC),**跳过 tpiu_demux**。
- 这对应 orbtrace core.py 的 **bypass 语义**(SWO 路径用的 `input_bypass`),只是我们对并行裸 ETM 也走"按 channel 1 直通、不 unmangle"的简化。

> 复用审计的最终落点:**4 个管线模块里,tpiu_demux 对我们这股裸流不适用(实测过它就毁数据);真正该复用的是 traceIF(已用)+ PC 端按规范锚定 I-sync(etm35lib,已实现并测试)。** SWAP_NIBBLES 留作参数,但当前 swap=0 + 不过 demux 才是能出真 PC 的路径。
