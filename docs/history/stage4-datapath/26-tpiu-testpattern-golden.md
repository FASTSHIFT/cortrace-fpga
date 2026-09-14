# 26 — TPIU 测试图案黄金对拍：采集链端到端零错

日期：2026-09-07
FPGA：`trace_ddr_stream_idelay24.bit`（IDDR + FIXED IDELAY tap24，已烧 QSPI FLASH），
STM32 @ R=4（56.25MHz pin）。

## 做法

用 **STM32 TPIU 内置测试图案发生器**（不经 ETM、不经 CPU，纯硬件、真实 trace 速率）
从引脚发**已知固定图案**，FPGA 走正常采集链（IDDR → DDR3 环形 → UDP）抓回来，逐字节
核对。这是我们一直缺的、比 LA 干净得多的**黄金参照**。

配置：`target/tpiu_testpattern_h743.cfg`，openocd 直接 poke TPIU 寄存器
（`CURTPM` @ 0x5C015204）。4-bit 并口，连续模式。

## 结果 —— 端到端 100% 干净

| 图案 | CURTPM | FPGA 抓回的不同字节值 | 结果 |
|------|--------|----------------------|------|
| AA/55 连续 | 0x00020004 | **1 个**：`0xA5` × 100%（111 MB） | 完美 |
| walking-1 | 0x00020001 | **2 个**：`0x21`/`0x84` 各 50%（111 MB） | 完美 |

- AA/55：每 lane 每个 TRACECLK 边沿都翻转，DDR 打包成单一 `0xA5`，**111,812,608 字节
  全部 0xA5，零其它值**。
- walking-1：`21 84 21 84 …` 严格交替，**零错、零丢、零毛刺**。

## 结论（周级调查的收敛点）

**从 STM32 引脚 → IDDR 采样 → DDR3 环形缓冲 → UDP 的整条 FPGA 采集链，在已知图案下
逐字节完美。** 这一次性证明了：

1. **采样前端（IDDR）没问题** —— 已知翻转图案下每 lane 每边沿零错。之前怀疑的
   "PE6/PE3 lane 掉位"彻底证伪（那是把真实 ETM 数据的自然统计误读成故障）。
2. **数据通路没问题** —— 再次确认（此前 ramp/fixed 已证，现在加上真实引脚输入）。
3. **hold/IDELAY/comp-cell 那些改动对错误率无影响，是因为采集链本来就没错**。STA 报的
   hold 违规是真的、修了也对（tap24 已固化），但它不是任何观测错误的原因。

**因此：之前用真实 ETM 数据看到的 ~17% 非法 Atom，100% 不在硬件采集，而在 PC 端
deframe/解码链。** 证据至此完全收敛——采集侧（FPGA/引脚/信号）全部排除，问题在软件
解帧。LA/示波器/DMA-GPIO 等一切采集侧的折腾就此可以停止。

## 复现
```
# 发图案（AA/55）
openocd -f interface/cmsis-dap.cfg -f target/stm32h7x.cfg \
        -f syn/artix7/bringup/target/tpiu_testpattern_h743.cfg -c shutdown
# 或 walking-1: mww 0x5C015204 0x00020001
# FPGA 抓
python3 trace_ctrl.py --iface enxc8a36266dcae rearm
sudo ./stream_grab enxc8a36266dcae 1 captures/tpiu.bin
# 期望：AA/55 -> 全 0xA5；walking-1 -> 0x21/0x84 交替
# 停止图案恢复正常: mww 0x5C015204 0  或 reload etm_enable_h743.cfg
```

## 下一步
掉头查 PC deframe（`opencsd_etm4_run.py` 的 nibble 重组 + TPIU 解帧）对上游
orbuculum `tpiuDecoder` 的实现差异 —— 那 17% 就在那里，且原始字节在手可反复复算。

---

## 附：同一 TPIU 图案下 LA 的表现（判死 LA 作为参照）

用逻辑分析仪数字 pod 抓**同一个 AA/55 图案**（FPGA 已证 111MB 全 0xA5、零错），
原始逐样点分析：

| | FPGA | LA（数字 pod, 1.25GS/s） |
|---|---|---|
| CLK runt 半周期 | 0 | **1162**（+283 long）|
| CLK 半周期分布 | 单一值 | 散在 8-15 采样点 |
| 数据 lane runt 边沿 | 0 | 189–3264 / lane |
| 数据边沿落在 CLK ±1/6 UI | — | **15–17%** |

图案是**已知完美**的（FPGA 铁证），所以 LA 报的每一个 runt/抖动都是 **LA 自己的伪象**
——数字 pod 阈值比较器在振铃/过冲上产生的假边沿。AA/55 让这点无可辩驳：真值是完美方波，
LA 却抖成这样。

**结论**：LA 数字 pod 不能作为这条链路的黄金参照——它自身的假边沿比被测信号的任何问题
都大。TPIU 硬件图案发生器才是正确的黄金源（FPGA 直接对拍，逐字节零错）。LA/示波器只能
当定性参考。此前 LA↔FPGA 对拍对不上、"CLK 有 53 个 runt"等，根源都在此。
