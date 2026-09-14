# r25 — H743 SSN 定责 + Medium-Slew 修复红方评审

**日期**：2026-07-12  
**对象**：`proposals/34-h743-medslew-ssn-fix.md`（LANDED 声明）+ 相关 RTL / 脚本 / cfg  
**结论口径**：**不能关闭为 landed**。修复本身可能真的短暂"工作"了一次，但**证据链有大量方法论漏洞**、gold-quality 声明是虚荣指标堆出来的、根因未真正定责。

---

## 一句话结论

> **"14/14 用户函数命中" 是虚荣指标，PASS 阈值只需要 9/14（60%），bitmap 是集合去重，捕获窗口把 very-high 卡在 20 ms、medium 拉到 200 ms 做对比 —— 这是一次未受控的、样本 N=1、跨 5× 时间窗、跨多个变量的一次幸运抓样，不是修复。** 提案 29 的架构结论（M7 drain 瓶颈需要*提高* TRACECLK）被本次悄悄绕过而无解释。

---

## R1 🟥 阻断 — 对照实验不干净，跨窗口比较 5:1

**证据**：`proposals/34-h743-medslew-ssn-fix.md` §四 表格：

| 阶段 | 窗口 | INSTR_RANGE |
|---|---|---|
| very-high slew | **20 ms** | 7 |
| medium slew | **100 ms** | 552 |
| medslew + orbetto | (未标) | 653 |

very-high 用 20 ms 抓样，medium 用 100 ms — **窗口时长差 5×**。表格里 medslew 20 ms 只有 121 INSTR_RANGE / 12/14，跟 very-high 20 ms 的 7/0 相比只提升 17× 是**样本 + 窗口耦合放大**的结果，不能作为"medslew 一击致命"的因果证据。缺失的对照格子是：
- very-high slew @ **100 ms** — 有没有可能也能达到 12/14 或 14/14？
- very-high slew @ **200 ms** — 未测。

**修复动作（root fix）**：
1. 用同一 firmware、同一 openocd session、同一 FPGA bitstream，对 OSPEEDR ∈ {00, 01, 10, 11} 各跑 **3 次 × 200 ms** 窗口，每次报告 (INSTR_RANGE, unique_PCs, hit/14, overflows)。
2. 建一个 `.perf` 存放到 `perf/functest_slew_sweep_{speed}_{trial}.perf`。
3. 得出的表要能证明 **medium 显著优于 very-high 且优于其他速度**，同时给出方差。

---

## R2 🟥 阻断 — PASS 阈值 = 60% (9/14)，"14/14" 是运气一次

**证据**：`decode/verify_func_test.py:101`

```python
ok = (len(inrange) == len(pcs)) and (len(hit) >= len(EXPECTED) * 0.6)
```

- `len(EXPECTED)=14` → 阈值 = `14*0.6 = 8.4` → 只需要 9 个函数就 PASS。
- 因此 `medium slew, 20 ms` 那行 12/14 也 PASS；`14/14` 只是 100 ms 那次的运气峰值。
- `bitmap.roar` 用 CRoaring **集合**记录 PC（`mortrall.hpp:830 r1.add(addr)`）。"看到过一次" 和 "看到 1000 次" 在 bitmap 里没区别。**误解码把一个偶然的 PC 落到某函数范围内**就算命中。

**修复动作**：
1. PASS 阈值改为 **14/14 强制**（用户函数不达全套即 FAIL）。
2. 增加 **调用顺序** / **调用次数** 判据：`level_a → level_b → level_c → leaf_mul` 应该是每 `main_loop` 迭代出现，比较**次数是否在期望区间**（比如迭代 N 次 → level_a 出现次数应 ∈ [0.9N, 1.1N]）。
3. 用 orbetto 生成 timeline (Perfetto JSON) 而不是只看 bitmap；`_add_pc` 之外应统计 `callStack` 深度事件，比对源码调用图。

---

## R3 🟥 阻断 — Overflow 63 直接被 mortrall 自己判定"corrupts Call Stack"

**证据**：`mortrall.hpp:280-285`

```cpp
printf("Overflows: %llu - %llu\n", cpu->overflows, cpu->ASyncs);
if (cpu->overflows > 0) {
    printf("Warning: When tracing implicit, it is very likely overflows corrupt the Call Stack.\n");
}
```

Orbetto 报告 `144 - 81 = 63` overflow，超过 0 = 触发官方警告，**调用栈已被腐蚀**。用户已经观测到 Perfetto 时间线"看着还是乱乱的"——这就是 overflow corruption 的直接表征。提案 §五轻描淡写"没影响 PC 集识别"是**移动球门柱**：从"call stack 正确"降级到"PC 集不空"，用集合去重掩盖时间线错乱。

**修复动作**：
1. `Overflows == 0` 应作为 LANDED 硬门槛，写进 `verify_func_test.py` 的判据。
2. 根因回到提案 29 §5：M7 的 drain 瓶颈需要**提高 TRACECLK 到 100 M+**，而不是拉低 slew。
3. 或者启用 `TRACE_STALL=1`（提案 34 的 medslew.cfg 保留了 0x10C，但仍报 63 overflow → **stall 不够狠或没生效**，需要读回 `TRCSTALLCTLR` 确认，并逐个测试 LEVEL=0/1/2/3 的 overflow 曲线）。

---

## R4 🟥 阻断 — 提案 29 → 34 的因果链自相矛盾，未做解释

**证据**：
- `proposals/29-H743-ETM溢出根因分析.md` §3.3、§5.A：结论**降 TRACECLK 帮倒忙**、修复方向是**提高 TRACECLK 到 100 M+**、"任何降低出口带宽的动作都是负优化"。
- `proposals/34` 完全不动 TRACECLK（仍 50 M），只降 GPIO slew。Slew 降 → 引脚边沿变缓 → **单位时间 drain 的 bit 数不变**（还是 50 M × 4 × 2 = 400 Mb/s）**但是 SI 余量变差**。

即便 medslew 表面 PC 数漂亮，**提案 29 判定的架构病灶（M7 生成 >> drain）没治**。63 overflow 就是死不掉的证据。

**修复动作**：
1. 在 proposal 34 里**显式论证**为什么绕过提案 29 的架构结论。
2. 至少给出**提高 TRACECLK 到 100 M** 的合成尝试结果作为对照 — 提案 29 §6 已列为下一步，不做等于回避。
3. 如果不做 100 M，就承认这不是 "gold quality"，最多是 "50 M with residual overflow corruption"。

---

## R5 🟨 高风险 — mww toggle 的 SSN 不能线性外推到 50 MHz TPIU

**证据**：`pin_multi_toggle.py:70-95`

mww 通过 SWD 每条 `mww 0x58021018 <val>` 大约需要 ~10 μs 传输 + Tcl 循环开销，粗估 **等效切换频率 ≤ 10 kHz**。TPIU 50 MHz 是 **5000× 更快**。

- SSN 峰值噪声正比于 `L·dI/dt`。**单次边沿**的 dI/dt 由 slew rate 决定，slew rate 在 mww 和 TPIU 场景一样（都是 OSPEEDR=11 时的 GPIO cell 特性），所以**单次事件峰值 SSN 可比**。
- **但**：SSN 的时间累积 / 电源退耦网络的响应 / 相邻边沿的相长干涉 在 kHz vs 50 MHz 完全不同。50 MHz 下电源网络处于连续激励，退耦电容更难跟上，**数据依赖**的多 lane 同 or 异 slot 切换分布决定实际 SSN。
- 提案 §2.3 声称 "线性放大到 40%" —— **没有物理依据**。真正的 TPIU 场景中 4 根 lane 都被主动驱动，"未驱动 pin 加 pull-down 观测耦合" 的 12.8% 是 **1 aggressor + 3 quiet victim** 结构，不代表 4-driver 场景的 D3 采样 40% 错误。

**修复动作**：
1. 用 `pin_speed_scan` 在 **ETM 真开、TPIU 真跑** 的场景下扫 OSPEEDR，测 D3 edge dirty% —— 不要用 mww 代打。
2. 或者：把 SSN 声明弱化为"定性排查方向"，明确说 **12.8% 到 40% 的换算是启发式，不是物理定律**。

---

## R6 🟨 高风险 — pin_speed_scan 的 46/69% 非单调结果高度可疑

**证据**：提案 §背景（Q A4）"OSPEEDR=10 (high) 独有 46/69% dirty 而 00/01/11 都 0.00%"。

搜索仓库没有找到该扫描的原始日志（`grep pin_speed_scan --include=*.log` 无匹配；`--outdir /tmp/pin_speed_scan` 是 tmpfs，日志已丢）。00 与 11 都干净、只有 10 脏 —— 这在硅片行为上讲不通（GPIO cell 的驱动能力和 slew 通常是单调递增的）。更可能是**测试脚本时序 bug**：

- `pin_speed_scan.py:150-158` 里 `_tgl` proc 定义完后立即 `time.sleep(0.05)` 就开始 LA capture，openocd Tcl 的 `mww` 循环还没稳定，可能在特定 speed 下命中 ring buffer 的 wrap 边界。
- `la_ddr_reader` 的 `wr_ptr - read_span` 计算 (`trace_pin_la_top.v:172-176`) 依赖 `wrptr_c1` 双寄存打拍，openocd Tcl 命令抖动会让不同 speed 落在 ring 的不同起点，抓到的窗口时间与预期不一致。

**修复动作**：
1. 复跑 `pin_speed_scan` 时同一 speed 抓 5 次，看 46/69% 是否稳定。
2. 保存原始 cap_{pin}_speed{sp}.bin 到 `perf/pin_speed_scan/` 而不是 `/tmp`，永久留证。
3. 如果无法复现 46/69% → 声明是脚本 bug，撤销 proposal 34 §2.4 的相关断言。

---

## R7 🟨 高风险 — 200 MSPS 采 50 MHz 的相位分辨率不够

**证据**：`trace_pin_la_top.v:87-92` 用 200 MHz clock（`clock u_clock` 的 clk_out1）作为唯一采样时钟；每 5 ns 一个样本。50 MHz TRACECLK 半周期 10 ns。

- `pin_la_analyze.py` 的 ±5 ns 窗口 = **1 个采样格**。宣称"D3 43.3% 边沿落在 CLK ±5 ns"等于"D3 边沿在 CLK 采样格附近"——**没有区分"边沿刚好在 CLK 之前 1 个格"和"边沿刚好在 CLK 之后 1 个格"**。真实的时序分辨率 = 采样周期 = 5 ns，**不足以支持 40% 抖动的定量结论**。
- 更严重的是采样是**异步**(200 MHz 与 TRACECLK 相位无关) + **双 flop 打拍**（`pin_s0 → pin_s1`）。50 MHz 沿相对 200 MHz 的位置是均匀分布的，观测到 40% 边沿落在 ±1 格里正好接近 **2/5 = 40%** —— 这可能是**采样几何的必然结果，不是 SI 抖动**。

**修复动作**：
1. 用 IDELAYE2 + 独立更高频率（400 M/500 M）过采样，或者用 IDDR + 边沿捕获模块，把有效时间分辨率提到 ≤2 ns。
2. 或者：改成"每个 CLK 周期采样两次（正沿 + 负沿），报告数据翻转是否发生在 CLK 边沿附近**且**跨过多个 CLK 周期"的**稳态相位**判据，而不是单点采样的"±5 ns 差异率"。

---

## R8 🟨 高风险 — la_byte 高 3 bit hardcoded 0，但捕获中被观察到非零 —— 有掩盖的 bug

**证据**：`trace_pin_la_top.v:111` 
```verilog
wire [7:0] la_byte = {3'b000, pin_s1};
```

高 3 bit 是常量 0。任何 la_byte >= 0x20 的字节都必然是**写入路径的错误**（可能是 la_ddr_writer 的 AsyncFIFO 溢出、128-bit 字段错位、DDR3 读回错位等），blue 方在 `pin_multi_toggle.py:105-110` 的 classify 里把高 bit 非零直接归为 "dirty"，然后声称是 SSN。**这是把 FPGA/DDR3 侧的 bug 算到硅片头上**。

commit `101dc79 fix(artix7): LA black-box word duplication — advance DDR3 addr +8 per burst word` 提到过 128-bit word duplication。这个 bug 是否**完全**修好了？没有 canary counter 佐证。

**修复动作**：
1. 在 `la_ddr_writer` 前给 la_byte 高 3 bit 加**递增 canary**（如 `wr_word_index[2:0]` 或 简单的 3-bit 计数器）。回读时验证 canary 序列连续，任何断裂 = FIFO / DDR3 bug。
2. 独立发一个 `--canary` 模式的 pin_wire_check_isolated，把 canary 通过后再报 SSN。
3. 提案 34 §2.1 那段"D3 1,968,822 edges (D0 的 8.5×)"—— 如果 D3 的高多余边沿部分来自 la_byte 高 bit "位串扰"（DDR3 write path 高 bit 错位）而不是 SI，整个定责链就 broken。

---

## R9 🟨 高风险 — 采样窗口的 CDC/reader 抖动可能是 speed=2 假象的来源

**证据**：`trace_pin_la_top.v:169-176`
```verilog
reg [28:0] wrptr_c0=0, wrptr_c1=0;
always @(posedge clk125) begin wrptr_c0<=wr_ptr_words; wrptr_c1<=wrptr_c0; end
wire [28:0] rd_start_addr = (wrptr_c1 >= READ_SPAN) ? ...
```

- `wr_ptr_words` 在 `ui_clk` (~200 MHz MIG clock) 域，`wrptr_c0/c1` 在 clk125 域。双寄存打拍 CDC 是安全的，但捕获**起点抖动**大概是 `1 / ui_clk = 5 ns × 几个 word` 量级。
- `arm_125` 来自 UDP 收到 `REG_ARM` 后的 `csr_we_w` 脉冲，UDP 收到时刻本身受到 host TX、Ethernet MAC RX FIFO 延迟影响，抖动量级 μs。
- 由 host `pin_speed_scan.py` 分别为每个 speed 单独 arm → 每次采样窗口落在真 trace 的**不同区段**，抓到的 dirty% 就是**采样起点采样**了 firmware 内不同的执行阶段（初始化 vs. main_loop 稳态）。

**修复动作**：
1. arm 时同时锁存一个 firmware 的时间锚（比如 SysTick 或 GPIOG_ODR heartbeat），后处理时对齐相同的 firmware 阶段。
2. reader 采用**同步 arm**（例如 firmware 内一段 known-pattern 出现在 pin 上时 FPGA 自动 arm），而不是依赖 host 一句 UDP。

---

## R10 🟨 高风险 — 修复裕度极小，明显没量化"medium 达到多少 dirty%"

**证据**：提案 §四 只给出**very-high**（D3 43.3% dirty）与**medium**（结果：14/14 PASS）两点，**没给出 medium 下 D3 的 dirty%**。宣称"从 40% 拉到 34% 跨过 OpenCSD 阈值"（问题描述里提到 34%）——**该数字在 proposal 和仓库均搜不到**（`grep -R "34%" proposals/34*.md` 无匹配）。

- 阈值也未定义：OpenCSD 什么时候开始丢 INSTR_RANGE？没有实测曲线。
- 换根杜邦线（长度差 2 cm）、换温度 +10 °C、换第二块 H743 die batch，slew 边沿再软一点点，就可能跌到"medium 也不够"。**这不是修复，是幸存者偏差**。

**修复动作**：
1. `pin_speed_scan` 在 ETM 真跑场景下，给出 OSPEEDR ∈ {00,01,10,11} 每个的 D3 dirty% 值，以及对应的 OpenCSD INSTR_RANGE / 14/14 命中比例。
2. 应答"多少 dirty% 是 OpenCSD 死点"这个问题，画出曲线。
3. 温度、多板卡 sanity check。

---

## R11 🟨 高风险 — GPIO 由 openocd 覆盖，运行时不 self-heal

**证据**：
- `func_test.c:377` firmware 用 `GPIO_SPEED_FREQ_VERY_HIGH` 只配 LED，trace pin 由 CubeMX 生成的初始化不管；实际 slew 靠 `etm_enable_h743_medslew.cfg` 在 openocd 运行时覆盖 `GPIOE_OSPEEDR`。
- 用户下一次上电 / 重新烧 firmware / 忘了跑 openocd cfg → OSPEEDR 又回 reset 值。
- 如果用户拿 CubeMX 重新生成 firmware（Speed=VERY_HIGH），trace pin 直接就是 very-high slew，跟目前"medslew fix"背道而驰。

**修复动作**：
1. **修 firmware** — 在 CubeMX 里把 PE2..PE6 配成 AF0 + MEDIUM speed，让 firmware 上电即处于目标状态。
2. openocd cfg 变成**验证** slew 是否正确，而不是设置它。
3. 或者：在 firmware 上电流程后一段固定代码里主动写 `GPIOE_OSPEEDR`，让重复上电稳定。

---

## R12 🟨 高风险 — F429 干净 / H743 需要 medslew 的物理差异未定责

**证据**：`proposals/25` F429 84 MHz 4-bit 干净；`proposals/34` H743 50 MHz 4-bit 需要 medslew。同板卡（A7-Lite）同杜邦线同 pin 出，但要不同 slew。

- H743 die 版本？GPIOE 电源分布？Package 内部差异？
- 芯片 die-yield / batch？
- H743 GPIOE 是否与更多外设共享同一段 VDDIO？
- 或者根本是 M7 双发射 + cache 生成端速度过快，TPIU 内部字节输出更密（同一 slew 下 SSN 也更强），这就回到提案 29 而非 slew。

**修复动作**：
1. 在 F429 上做同款 `pin_multi_toggle` 测 D3 SSN，做同款 `pin_speed_scan` 测 dirty%，看 F429 是否天然 dirty% 很低。
2. 或者：给 H743 vs F429 的 datasheet 里对比 GPIOE 部分的 IO cell max drive strength / decoupling recommendation。
3. 如果没有物理解释，就诚实标注 "medslew 是启发式经验解，机理未定"。

---

## R13 🟩 轻微 — Gold reference 覆盖率对比是水的

**证据**：提案 §四声称"gold 覆盖率已达标"，但从未给出 F429 84 M gold reference 的**逐项**对比（PC 数、事件数、时间跨度、每个用户函数 PC 计数）。

- 提案 27 记录 F429 84 M gold ≈ **5672 PCs、56K events**。
- H743 medslew ≈ **653 PCs**（**8.7× 少**），50 M vs 84 M 采样带宽约差 1.7×，无法解释 8.7×。
- 51 functions covered 里包含 CubeMX 生成的 HAL_Init / HAL_PWREx / SystemInit / HAL_RCC_*，这些是**上电初始化必经**路径，SC 掉了它们仍然 healthy。真正 discriminative 的是 func_test.c 里 14 个用户函数**间的调用关系** —— 目前没有验证。

**修复动作**：
1. 增加 `verify_func_test.py` 输出：**每个用户函数的 PC 计数**、**调用图边**（从 orbetto FTrace 事件重建），与源码 static call graph 做 diff。
2. 与 F429 gold 事件数做同 firmware 迭代次数下的比较，报告 event density ratio。

---

## R14 🟩 轻微 — pin_wire_check 的通道拓扑测试没意义

**证据**：`pin_wire_check.py` 每次只驱动一根 pin，其他 pin 加 pull-down，报告 CLK / D0..3 edges。这**只验证 board wiring**（哪根 pin 走到哪个 FPGA 通道），但对 SI / SSN / eye 都无信息量。

被 blue 方拿来作为"LA 本身可信"的证据 —— 这个论断成立，但**只证明了 LA 单 pin 场景可信**，不代表 LA 在多 pin 同 slew rate 场景下没有采样窗口 miss。

**修复动作**：不必修，但不要把这个测试挂在"SSN 定责证据链"里当锚。

---

## R15 🟨 高风险 — TRCSTALLCTLR=0x10C 有 63 overflow → 该值不是最严格 stall

**证据**：`etm_enable_h743.cfg:225` `mww 0xE004102C 0x0000010C` bit[8] ISTALL=1, bit[3:2] LEVEL=11 (max)。仍然 63 overflow → 说明 stall 机制**没生效**或**该 ETM-M7 实现无效**。可能：

- LEVEL 的编码不同版本 ETMv4 略有差异，DDI0494D §3.4.7 表格需二次核对。
- ISTALL 触发要求：indicator ETF 报告"低水位"给 ETM，H743 的 ETF 和 ETM 之间的信号可能没连（CSTF 中间嵌了一级）。
- 也可能"stall 生效但 M7 只是短暂停顿一 cycle，跟 63 overflow 无关"。

**修复动作**：
1. 读回 `TRCSTATR` bit STALLED，看是否有 stall 事件；`TRCSSCSR0` 状态。
2. 分别测试 LEVEL=0/1/2/3，看 overflow 曲线。
3. 如果 stall 完全无效，考虑 M7 stall 需要 D-cache write-back drain 完成后才能生效，可能与 cache 交互 —— 提案 29 的方向仍是主线。

---

## 总评

### Verdict：**NOT LANDED**

- **一次成功抓样 ≠ 稳定修复**。用户口述"看着还是乱乱的"和 63 overflow 已经戳破 gold quality 声明。
- proposal 33 撤回是正确的；proposal 34 目前应该降为 **"observation & interim workaround"** 状态，不能作为 landed。

### Next-step 优先级

- **P0**（不做则不 landed）：
  - R1 干净对照实验（same window, N≥3）
  - R2 PASS 阈值收紧到 14/14 + 调用图 diff
  - R3 Overflow=0 硬门槛（这就把 R4 和 R15 一起逼出解决）
  - R8 la_byte canary，验证 FPGA / DDR3 侧无残余 bug

- **P1**（提高置信度）：
  - R5 SSN 线性外推的物理论证或弱化措辞
  - R6 pin_speed_scan 46/69% 复现或撤销
  - R7 采样分辨率提升（400 M 过采样 或 IDDR 双沿）
  - R10 medium slew 下 dirty% 量化 + 阈值曲线

- **P2**（长期健康）：
  - R11 firmware 侧自愈 slew
  - R12 F429 vs H743 物理差异定责
  - R13 gold reference 逐项对比
  - R9 CDC / arm 起点稳定化

### 一句话给用户

**别急着 close 这个 proposal —— "14/14 PASS" 的 PASS 判据只需要 9/14 且是集合去重，用 200 ms 窗口和 20 ms 窗口比出来的 17× 是窗口在放大不是 slew 在修，overflow=63 已经被 mortrall 自己判定"腐蚀调用栈"，你眼睛看到的"乱乱的" perfetto 就是这条证据。medslew 顶多是个短期 workaround，先按 R1–R4 打稳。**
