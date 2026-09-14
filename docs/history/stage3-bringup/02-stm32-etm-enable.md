# Stage-3 · STM32F429 ETM 并行 Trace 使能（被测对象自验）

> 目标：在**不接 FPGA** 的前提下，用 ST-Link + OpenOCD 配好 STM32F429 的 ETM →
> TPIU → 4-bit 并行 trace 端口，用示波器确认 TRACECLK/TRACED 真有信号。
> 这是红方从 r02 起反复要求的"先把被测对象可疑度降下来"——trace 工具调试时，
> 先证明"STM32 会发数据"，再去查"FPGA 能不能收"。
>
> **结果：通过。** PE2 (TRACECLK) + PE3..PE6 (TRACED0..3) 输出 trace 数据。
>
> 环境：STM32F429I-DISC1（板载 ST-Link/V2.1）+ OpenOCD 0.12.0（`hla_swd`）。

---

## TL;DR

配 ETM 并行 trace 卡了两轮，根因都不是 ETM 本身：

| # | 漏配 | 症状 | 修复 |
|---|------|------|------|
| 1 | **GPIOE 时钟未开 + 引脚没切到 AF0 复用** | TRACECLK/TRACED 全程无波形 | 手动配 GPIOE：RCC 时钟 + MODER=10(AF) + AFRL=0(AF0) + 高速 |
| 2 | **只配了 TPIU，没使能 ETM** | TPIU 是空管道，无数据源 | ETM_LAR 解锁 + ETMCR + ETMTEEVR + ETMTECR1 + ETMTRACEIDR |

**最大的坑**：`DBGMCU_CR.TRACE_IOEN` **不会自动**把 GPIOE 切到 trace 复用功能——必须手动配 GPIO 的 MODER/AFR。很多教程把这步省了（因为它们用 Keil/STM32CubeMX 自动生成 GPIO 配置），裸 OpenOCD 配寄存器时这步绝不能漏。

诊断关键一步：先用 **GPIO 输出翻转 PE2** 确认引脚物理通路 + 示波器探测点 OK，再去查 trace 配置——避免在"是配置错还是探错地方"之间瞎猜。

---

## STM32F429I-DISC1 trace 引脚（AF0）

| 信号 | 引脚 | DISC1 可用性 |
|------|------|-------------|
| TRACECLK | PE2 | ✅ UM1670 确认 PE2..PE6 空闲引出，无外设占用 |
| TRACED0 | PE3 | ✅ |
| TRACED1 | PE4 | ✅ |
| TRACED2 | PE5 | ✅ |
| TRACED3 | PE6 | ✅ |

---

## 完整使能序列（OpenOCD）

见 `syn/artix7/bringup/etm_enable.cfg`。关键寄存器分四组：

### 组 0：GPIOE 复用（最容易漏，也是第一个真因）
```tcl
# RCC_AHB1ENR bit4 = GPIOEEN（不开时钟，引脚死的）
mww 0x40023830 [expr {[mrw 0x40023830] | 0x00000010}]
# MODER: PE2..PE6 = 10b（复用功能）-> 0x2aa0 区
# OSPEEDR: PE2..PE6 = 11b（超高速）
# AFRL:  PE2..PE6 nibble = 0（AF0 = trace）
```
回读确认：`GPIOE_MODER = 0x00002aa0`、`GPIOE_AFRL = 0x00000000`。

### 组 1：核心 trace 上电
```tcl
mww 0xE000EDFC 0x01000000   ;# DEMCR.TRCENA (bit24)
```

### 组 2：DBGMCU
```tcl
mww 0xE0042004 0x000000E0   ;# TRACE_IOEN(bit5) + TRACE_MODE=11 (4-bit parallel)
```

### 组 3：TPIU（格式化/串化漏斗）
```tcl
mww 0xE0040004 0x00000008   ;# CSPSR: 4-bit port
mww 0xE00400F0 0x00000000   ;# SPPR : parallel (sync)
mww 0xE0040010 0x0000000F   ;# ACPR : /16 prescaler（调慢，示波器友好；满速时设 0）
mww 0xE0040304 0x00000102   ;# FFCR : EnFCont 连续格式化
```

### 组 4：ETM（数据源 —— 漏了它 TPIU 就是空管道）
```tcl
mww 0xE0041FB0 0xC5ACCE55   ;# ETM_LAR 解锁
mww 0xE0041000 0x00000410   ;# ETMCR: ProgBit(10) + 4-bit port（进编程态）
mww 0xE0041200 0x00000001   ;# ETMTRACEIDR = 1
mww 0xE0041024 0x00000000   ;# ETMTECR1 = 0（全地址范围 trace）
mww 0xE0041020 0x0000006F   ;# ETMTEEVR = always TRUE
mww 0xE0041000 0x00000010   ;# ETMCR: 清 ProgBit，使能运行
```
回读确认：`ETMCR = 0x00000010`（ProgBit 已清，ETM 运行中）。

---

## 验证方法

```bash
openocd -f interface/stlink.cfg -f target/stm32f4x.cfg \
        -f syn/artix7/bringup/etm_enable.cfg
# 保持运行，示波器探 PE2
```

预期：
- **PE2 (TRACECLK)**：稳定时钟，频率 = TRACECLKIN / (ACPR+1)。固件在 HSI 16MHz 时，/16 prescaler 下约 1MHz 量级；固件开 PLL 后更高。
- **PE3..PE6 (TRACED0-3)**：CPU 执行指令时随机翻转（DDR trace 数据）。

满速测试时把 `TPIU_ACPR` 设回 0（TRACECLK = 全速）。

---

## 调试时间线（诚实记录）

1. 第一版：只配 DEMCR + DBGMCU + TPIU configure/enable → PE2 无波形。
2. 怀疑寄存器没写进去 → 用 `mrw` 回读，确认 DBGMCU_CR=0xe0 写进去了，但仍无波形。
3. 第二版：补了 ETM 使能 + RCC GPIOE 时钟 → 仍无波形。
4. **关键隔离**：把 PE2 配成普通 GPIO 输出，loop 翻转 ODR → **示波器看到方波**。证明引脚 + 探测点 OK，问题在 trace 复用配置。
5. 定位真因：`DBGMCU_CR.TRACE_IOEN` 没有自动把 GPIOE 切到 AF0；手动配 MODER=10 + AFRL=0（AF0）+ 高速。
6. 第三版：补齐 GPIO 复用 → **TRACECLK + TRACED 出数据**。✅

---

## 这一关验证了什么

| 验证项 | 状态 |
|--------|------|
| ST-Link SWD 调试链路 | ✅ Cortex-M4 r0p1 检测、halt/resume |
| ETM/TPIU 寄存器可读写 | ✅ 全部回读确认 |
| trace 引脚物理输出（GPIO 自测） | ✅ PE2 GPIO 翻转可见 |
| **ETM 4-bit 并行 trace 输出** | ✅ TRACECLK + TRACED 有信号 |

**意义**：被测对象（STM32F429）确认会从 trace 引脚吐数据。后续把 5 根线接到
A7-Lite，FPGA 收不到数据时，可以排除"STM32 没发"这一项——可疑度集中到
FPGA 采样侧（IDELAY/eye/deskew）。这正是红方一贯强调的"先自验被测对象"。

---

## 下一步（Stage-3 续）

1. 把 TRACECLK + TRACED0-3 + GND 5 根线从 DISC1 的 PE2..PE6 接到 A7-Lite 的
   GPIO1 trace 引脚（TRACECLK→D17 MRCC）。
2. 先低速（大 ACPR prescaler）跑，FPGA 侧 ILA 抓 trace_a/trace_b，确认采到稳定数据。
3. 扫 IDELAY tap 找眼图中心（deskew），逐步提速到满速。
4. traceIF 解出 TPIU 帧 → 千兆网 → PC orbuculum 解码。
