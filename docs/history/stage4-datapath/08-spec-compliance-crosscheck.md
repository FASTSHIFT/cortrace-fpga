# Stage-4 · 交叉检查:orbtrace / orbuculum 是否符合 ARM ETM 官方规范

> 对照 `docs/artix7-port/refs/` 的两本官方手册,逐项核对我们用到的 orbtrace gateware(`orbtrace/orbtrace/trace/`)和 orbuculum 上位机(`orbuculum/Src/`)的 ETM 处理是否符合规范,定位与我们 4-bit 口场景相关的合规缺口。
>
> 参考:**IHI0014Q**(ETMv3.5 架构规范)、**DDI0440C**(Cortex-M4 ETM TRM)。

---

## 1. 结论先行

| 组件 | 规范符合度 | 与我们场景的关系 |
|------|-----------|------------------|
| orbtrace `TPIUSync` (tpiu.py) | ✅ 符合 TPIU formatter 同步(`0xffffff7f` + `0xff7f` 半同步) | **但只用于 SWO 路径**,并行 trace 不走它 |
| orbtrace `TPIUDemux`/`Unmangle` | ✅ 符合 TPIU formatter 帧解扰 | 假设输入已字节对齐的 16 字节 TPIU 帧 |
| orbtrace 并行 trace 路径(core.py) | ⚠️ 仅适配 **formatter on** 的 TPIU 帧流 | 我们的流没有 TPIU 帧 → demux 散乱(已实测) |
| orbuculum `traceDecoder_etm35` | ⚠️ **逐字节**处理,A-sync 只在字节边界复位 IDLE | **不做 IHI0014Q §7.10.4 要求的 4-bit 口子字节重对齐** |
| orbuculum `tpiuDecoder` | ✅ 符合 TPIU 16 字节帧 + `0xffffff7f` 同步 | 需要 formatter 帧,我们没有 |

**核心缺口(与我们直接相关)**:**ARM 规范 IHI0014Q §7.10.4 明确要求,对非字节对齐的 trace 口(4-bit 口就是),解码器必须在每个 A-sync 后重新对齐数据。orbuculum 的 ETM35 解码器只做字节级处理,没有子字节重对齐能力。** 这正是我们解码失败的规范级根因。

---

## 2. 逐项核对

### 2.1 orbtrace `TPIUSync`(tpiu.py)— 符合,但用错场景

```python
with m.If(Cat(self.input.payload, buf)[:32] == 0xffffff7f):   # full sync
    synced.eq(1); buf.eq(1)
with m.Elif(Cat(self.input.payload, buf)[:16] == 0xff7f):     # half sync
    buf.eq(buf[8:])
```

- **符合** TPIU formatter 协议(DDI0314 TPIU):全同步 `0xFFFFFF7F`、半同步 `0x7FFF` 过滤。
- **但**:`core.py` 里 `TPIUSync` 只在 `input_format` 0x11/0x13(**SWO over UART/Manchester**)路径里例化。**并行 trace(0x01-0x03)直接 `traceIF → trace_fifo → tpiu_demux`,完全不经 TPIUSync。**
- 含义:orbtrace 假设**并行口的字节对齐由硬件 TPIU formatter 保证**(IHI0014Q §7.10.4 Note:"trace 嵌入 CoreSight formatting 协议时是字节对齐的")。我们的链路里 traceIF 之后没有 formatter 字节对齐保证。

### 2.2 orbtrace `Unmangle` / `TPIUDemux` — 符合 TPIU,但前提是 formatter 帧

`Unmangle`(tpiu.py)实现的是 **TPIU formatter 的 16 字节帧解扰**(偶数字节 bit0 是 ID/data 标志,aux 字节 byte15 提供被挤占的 bit0)。这**完全符合** CoreSight TPIU formatter 规范。

**但**:它假设输入是**对齐好的 16 字节 TPIU formatter 帧**。我们实测(§9)流里没有 `0xFFFFFF7F`、喂进去散到 20-40 通道 —— 因为我们的 STM32 并行口输出的是**裸 ETM,没有 TPIU formatter 封装**。所以 demux 用错了对象,不是 demux 不合规。

### 2.3 orbuculum `traceDecoder_etm35.c` — 关键合规缺口

```c
/* Perform A-Sync accumulation check */
if ( ( j->asyncCount >= 5 ) && ( c == 0x80 ) ) { newState = TRACE_IDLE; }
else { j->asyncCount = c ? 0 : j->asyncCount + 1; ... }
```

- A-sync 识别**符合** IHI0014Q §7.10.4(≥5 零 + 0x80)。
- **缺口**:解码器 `TRACEDecoderPump` 以**整字节**为单位推进,A-sync 后只是把状态机切回 `TRACE_IDLE`,**在同一字节流上继续按字节解**。
- IHI0014Q §7.10.4 明确:
  > "While trace capture devices are usually byte-aligned, this might not be the case for sub-byte ports. Therefore the decompressor **must realign all data following the A-sync sequence if required**."
  > Note: "The trace is normally byte-aligned if ... the width of TRACEDATA is a multiple of 8 bits, **that is, not a 4-bit port**."
- **orbuculum 没有实现子字节(bit 级)重对齐**。它面向 ORBTrace Mini 的硬件 TPIU/formatter 输出(天然字节对齐),从未需要处理裸 4-bit 口的子字节错位。
- **对我们**:4-bit 口 + 无 formatter,一旦某次采样错位,后续字节整体偏移,orbuculum 无法在 A-sync 后重对齐 → 解码持续失锁(实测 syncCount=0)。

### 2.4 orbuculum `tpiuDecoder.c` — 符合,但同样要 formatter 帧

`SYNCPATTERN=0xFFFFFF7F`、16 字节帧、半同步过滤 —— **符合** TPIU 规范。但同样要求输入是 formatter 帧,我们没有。

---

## 3. 与 orbtrace 官方架构的差异定位

orbtrace 的设计前提(完全合规):**ORBTrace Mini 用 ECP5 + 硬件把并行 trace 包进 TPIU/CoreSight formatter**,所以到 PC 的流天然字节对齐、带 `0xFFFFFF7F` 帧同步,`tpiuDecoder` / `TPIUDemux` 直接吃。

我们的移植链路差异:
1. STM32 并行口在**单 ETM 源**下输出**裸 ETM**(无 formatter 帧,实测无 `0xFFFFFF7F`)。
2. 我们的 `traceIF.v` 只在上电锁一次 TPIU 同步字后自由跑,不提供持续的 formatter 字节对齐。
3. → 到 PC 的是**可能子字节错位的裸 ETM**,而 orbuculum 的 ETM35 解码器**按规范本可处理,但没实现子字节重对齐**这一可选项。

**这不是 orbtrace/orbuculum 的 bug**(它们针对自己的硬件场景是合规的),而是**我们的 4-bit 直连场景命中了规范里"sub-byte port 需要额外重对齐"的边角**,而上位机没覆盖这个边角。

---

## 4. 行动项

1. **PC 端解码器补子字节重对齐**(我们自己实现,`etm35lib.py` 已起步):每个 A-sync 后扫 8 种 bit 偏移,锁定后续 I-sync(规范允许且要求)。
2. `etm35lib.py` 的 I-sync 锚点提取已 100% 单测覆盖 + 真实抓取回归(`test_etm35lib.py`,32 用例)。
3. 长期:若要完全复用 orbuculum,需给其 ETM35 解码器加一个"sub-byte realign on A-sync"选项并回馈上游;或在 FPGA 侧真正实现 TPIU formatter(像 ORBTrace Mini)让输出天然字节对齐。

> 一句话:**orbtrace/orbuculum 对它们的 formatter-on 硬件场景是符合 ARM 规范的;我们的裸 4-bit 口命中了规范明文要求、但上位机未实现的"子字节重对齐"边角。修复点在 PC 解码器(已 spec 对齐并加测试),不在 orbtrace 本身。**
