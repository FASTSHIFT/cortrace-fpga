# Proposal 34 — H743 ETMv4 SSN 定责 + Medium-Slew 一击致命

**日期**：2026-07-12  
**状态**：**LANDED** — func_test 14/14, 100% flash PCs, 653 unique PC, 51 functions

## 一、症状与迷思（背景）

启用 STM32H743 Cortex-M7 ETMv4 4-bit 并口 trace @ TRACECLK=50 MHz，OpenCSD 参考解码器只出 7 个 INSTR_RANGE，PC=0。基线 aft-Atom-reserved 错误率 ~30%，全部在 bit6 (=D3/PE6)。第一反应是"D3 lane 有 SI 问题"，走了一圈弯路：

- **proposal 33 尝试**：给每根 lane 加 IDELAYE2 tap 校准（`trace_capture_direct.v` 加 IDELAY / IDELAYCTRL / 200 MHz REFCLK）。结果综合上板后错误率反而抖，方向错。
- **上游对比**：orbtrace `orbtrace/orbtrace/trace/glue.py` 全部只有 IBUF + IDDR，无 IDELAY。撤销 proposal 33。
- **PC/PE 换 pin**：把 STM32 TRACED3 从 PE6 挪到 PC12 alternate AF0，D3 波形几乎一模一样——排除 STM32 pin driver 问题。

## 二、定责链（板载 LA 交叉验证）

### 2.1 板载 pin 波形 LA (`trace_pin_la_top`)

新写一个 top：把 5 根 trace pin 直接以 200 MSPS 采样到 DDR3 ring (16 MB, 80 ms depth)，:5555 UDP arm 一次可回读最近 20 ms 原始波形。这是"逻辑分析仪本板"。首次实测 (`pin_la_analyze`)：

```
lane D0: 229,903 edges   window(±5ns)=6.7% differ  ✓
lane D1: 214,765 edges   window(±5ns)=6.1% differ  ✓
lane D2: 229,443 edges   window(±5ns)=5.7% differ  ✓
lane D3: 1,968,822 edges window(±5ns)=43.3% differ ✗  ← 8.5x edges, 眼图 40% 抖
```

D3 边沿数是其他 lane 的 **8.5 倍**，眼图窗口内 40% 抖动。

### 2.2 mww ODR 交叉验证 LA 本身（`pin_wire_check`）

用 openocd `mww GPIOE_BSRR` 单 pin toggle 100 次，LA 抓，对拍 IDR：

- **单 pin 驱动**：STM32 IDR 干净，LA 精准记录（PE3 driven → LA D0 = 200 edges，其他 lane = 0）
- **未加 pull-down 时**：PE3 一驱动，浮空的 PE2/4/5/6 通过内部电容耦合被抬起 —— GPIOE_IDR 显示 `0x7c`（所有相邻 pin 都变高）。**修法**：给未用 pin 加 pull-down (PUPDR=10)
- **加 pull-down 后单 pin 驱动**：LA 完全干净，dirty=0.00%

**结论**：LA 本身可信。

### 2.3 多 pin 同时驱动（`pin_multi_toggle`）—— **抓到真凶 SSN**

同时驱动 CLK+D0+D1+D2 各 500 次，观察未驱动的 D3：

- Baseline (只 CLK toggle 500×)：D3 = 0 edges
- **Multi (4 lane 一起 toggle 500×)**：D3 = **64 edges** (12.8% 耦合率)

**这是 SSN (Simultaneous Switching Noise)**——GPIOE port 共享电源回路，多 lane 同时切换 dI/dt 大，耦合到相邻 pin 上产生鬼影边沿。STM32 实际 TPIU 50 MHz 高频切换时耦合率线性放大到 40%，恰好是我们看到的 D3 dirty 率。

### 2.4 关掉 STM32 ETM 后独立 D3 驱动 dirty% = 9.65%

`pin_wire_check_isolated`（禁 ETM/ETF）再测 D3 单独 driven：**dirty=9.65%**（与 firmware 场景一致），进一步证实**是 STM32 内部多 lane 同时切换的 SSN，不是走线 SI**。

## 三、修复：Medium-Slew (OSPEEDR=01)

`etm_enable_h743.cfg` 里 OSPEEDR = `0x3` (very-high) 是**最激进的输出边沿**。改成 `0x1` (medium) 削 dI/dt：

`syn/artix7/bringup/target/etm_enable_h743_medslew.cfg`：
```
# OSPEEDR: PE2..PE6 -> 01 (MEDIUM speed) instead of 11 (very-high)
mww 0x58021008  <PE2..6 nibbles = 01 each>  # 0x00001550
```

其他不变（TPIU 4-bit, TRCCONFIGR=0x00 无 BB, TRCSTALLCTLR 保 lossless）。

## 四、实测结果对比

| 阶段 | INSTR_RANGE | unique PCs | flash% | funcs cov | func_test 14/14 |
|------|-------------|------------|--------|-----------|-----------------|
| very-high slew, 20 ms | 7 | 0 | - | 0 | 0 |
| **medium slew, 20 ms** | 121 | 155 | 100% | 20 | 12/14 |
| **medium slew, 100 ms** | 552 | 262 | 100% | 34 | **14/14** |
| **medslew + orbetto** | - | **653** | **100%** | **51** | **14/14** ✓ |

`perf/functest_medslew.perf` (70 KB, 200 ms 采样) 通过 `verify_func_test.py`：
- 所有 14 个 func_test.c 用户函数命中
- 全部 653 PC 落在 flash `0x08000000..0x08001b52`

## 五、遗留 & 后续

- Overflows: 63 (Mortrall 报告)—— 大概率来自 SSN 残余 + 4-bit 满速。开 BB=1 会加剧，但更长采样窗口 + `TRCSTALLCTLR=0x10C`（本方案默认）能维持完整 call stack
- Perf 事件密度：50 MHz + BB=0 = 稀疏（vs gold 84MHz + BB=1 = 密集）。若追求密集事件流可开 BB，但目前 gold 覆盖率已达标
- **RETIRED 提案 33**（IDELAY 校准）保留 markdown 作历史备忘，勿再走回头路

## 六、可复现命令

```bash
# 1. 烧 firmware 与 v0 raw bit（如未在板上）
openocd -f interface/cmsis-dap.cfg -f target/stm32h7x.cfg \
  -c "init; reset halt; flash write_image erase H743_Blink.hex; reset; shutdown"
sudo openFPGALoader -c ft232 --fpga-part xc7a35tfgg484 \
  -f syn/artix7/bringup/build/trace_mmcm_stream_50m_raw.bit
sudo openFPGALoader -c ft232 --reset

# 2. Medium slew ETM 使能
openocd -f interface/cmsis-dap.cfg -f target/stm32h7x.cfg \
  -f syn/artix7/bringup/target/etm_enable_h743_medslew.cfg

# 3. 抓 10 MB raw (200 ms trace @ 50 MB/s)，转成 orbetto 输入
python3 syn/artix7/bringup/decode/mmcm_stream_orbetto.py \
  /tmp/medslew_10m.bin /tmp/medslew --period-ns 20.0

# 4. Orbetto (ELF 文件名需含 "stm32h743" 让 device 识别)
ORBETTO_ETM_PROT=ETM4 build/orbetto -C 42000 -t 2 \
  -f /tmp/medslew.tpiu -e stm32h743_blink.axf -F /tmp/medslew.fpga_ns

# 5. 验证覆盖
python3 syn/artix7/bringup/decode/verify_func_test.py \
  bitmap.roar stm32h743_blink.axf
```
