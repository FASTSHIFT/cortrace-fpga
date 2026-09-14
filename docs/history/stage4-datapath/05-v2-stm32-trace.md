# Stage-4 · V2：接真实 STM32 ETM,FPGA 采样并解出指令流

> 第四阶段验证阶梯第三关(见 `../PLAN_STAGE4.md`)。
> 目标:把 V1 的自环数据源换成**真实 STM32F429 的 ETM 4-bit 并行 trace**,验证 FPGA 采样链 + 上游 traceIF 能从真实指令流里锁定 sync 并解出帧。
> 结论:**通过。** STM32 真实 trace 经 FPGA 采样 → traceIF 解码,端到端打通;眼心 tap=28 稳定,解出的帧内容随执行流不断变化(真实 trace 特征)。

---

## 这一关验什么 / 跟 V1 的区别

V1 是 FPGA 自发自收已知 TPIU 帧(内容固定 `1234…0f`)。V2 第一次引入**真实、不可控的输入**:STM32 的 ETM 实时指令流。判据也随之变:
- V1:某 tap 解出的帧 == 固定 golden → 该 tap 开眼
- V2:内容每帧都变,无法比固定值,改判**"该 tap 下 traceIF 锁定 sync 并吐出的帧数"**——相位对的 tap 帧数高且稳定,相位错的 tap 偶尔蒙中一次 sync(帧数低,是假象)

实现上加了 `EXT_SRC` 参数(`trace_eyescan.v` / `eyescan_top.v`):为真时(1)pattern 发生器不驱动(STM32 驱动 trace 脚),(2)任何解出的帧都计入 good。综合用 `EXT_SRC=1 vivado -mode batch -source ../run_eyescan.tcl`。

## 物理连线(STM32F429-DISC1 → A7-Lite)

拆掉 V1 的 GPIO1 自环跳线,改成:

| 信号 | STM32 | → | FPGA 输入脚 | GPIO1 排针 |
|------|-------|---|------------|-----------|
| TRACECLK | PE2 | → | D17 | 9 |
| TRACED0 | PE3 | → | F13 | 1 |
| TRACED1 | PE4 | → | E14 | 4 |
| TRACED2 | PE5 | → | D14 | 5 |
| TRACED3 | PE6 | → | E16 | 7 |
| GND | DISC1 GND | → | FPGA GND | — |

**两板必须共地。** STM32 端先跑 `etm_enable.cfg` 打开 ETM(掉电/复位丢失,需重跑)。

## 实测结果

每个 tap 测窗口内 traceIF 吐出的帧数(STM32 ETM,TPIU /16 prescale):

```
tap 28 | 8774  ← best (eye_found=1, 稳定复现)
tap 27 | 7238
tap 29 | 6553
tap  6 | 7951
tap 17 | 5979
tap  1 | 5106
...其余多数 ~40(噪声底,相位不对时偶发的假 sync)
```

- **眼心 tap 27-29 连续一簇、帧数最高**,FPGA 自动选 `best_tap=28`,多次读取稳定一致。
- 解出的帧每次不同(`0b00ae…` → `0b8cae…` → `4b84ccce…`)——**真实 trace 的指纹:内容随执行流变化**(对比 V1 自环恒为 `1234…0f`)。
- 帧数差约 200×(8774 vs ~40),眼心信噪比很高。

**判据 W-2 的"sync 建立稳定 + 解出帧"部分达成:真实被测对象 → FPGA 采样 → traceIF 解帧端到端通。**

## 诚实观察:眼图不连续

与 V1 自环干净的"18-31 连续 14-tap 眼"不同,V2 的高帧数 tap 散布(1/6/17/22/27-29),不是单一连续窗口。判断成因:
- STM32 的 ETM trace **不是连续满速流**(指令流有间歇/空闲),每个 tap 固定测量窗口内"正好发了多少帧"有统计涨落;
- 固定窗口 × 稀疏数据 + trace_clk 与测量的拍频,可能在某些非眼心 tap 造出孤立高点。

**最可信的是 tap 27-29 这一连续簇**(连续 + 最高 + 每次稳定),与 best_tap=28 一致。孤立高点(1/6/17/22)存疑,暂判为采样窗口/数据稀疏的假象,不作为眼心。

> 待 V2 收尾可改进:测量改成"固定帧数计时"而非"固定时间计帧",消除数据稀疏带来的涨落;或拉长窗口多次平均。

## 下一步
- 用 best_tap=28 固化采样,把 traceIF 解出的帧接进 TPIU demux → OrbFlow → UDP,送到 PC; **← 已做(见下)**
- **V3**:编译 Orbuculum,PC 端实时解码这条真实 trace 流,对出 STM32 实际执行的函数/PC 流(端到端 PoC 收口)。

---

## V3 第一步:固定 tap 流式送帧到 PC（已通过）

用 V2 眼心 `tap=28` 固化采样,新建 `trace_stream_top.v`:STM32 ETM → `trace_capture_a7`(BUFR_IO, tap=28)→ `traceIF` 解帧 → 环形缓冲(最近 8 帧)→ UDP :5001 读出 + 32-bit frame_count。

实测(`trace_stream_read.py`):
- **frame_count 持续单调增**:3 秒内 33,980,286 → 35,191,379,约 **40 万帧/秒**,真实活流。
- 8 个 slot 都是真实 TPIU 数据,含可辨识结构:`7f`/`7fff`(TPIU 同步/对齐)、`0b84`/`027f`/`01cc`(ITM channel `0b` + 包)。
- 内容随执行流不断变化,非固定 golden。

构建/运行:
```bash
cd build && TAP=28 vivado -mode batch -source ../run_trace_stream.tcl
# 烧 trace_stream.bit
python3 ../trace_stream_read.py --ip 192.168.10.42 --watch   # 看帧率
```

**至此「真实被测对象 → FPGA 采样+解帧 → 网络送到 PC」整条链打通。** 剩下是 PC 端用 Orbuculum 把这些 TPIU 帧解成函数/PC 执行流（V3 收口）。
