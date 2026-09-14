# 提案 22:Artix-7 采样方案的频率分工 —— IDELAY 移相范围决定的采集频率下限

> 触发:2-bit/4-bit 并口 trace 上板调试中,过采样(OVERSAMPLE)在 168MHz 主频下
> **过采样重复失锁**,降频到 1.3MHz 后**首次在 FPGA 通路上解出真实 PC**。本文把
> 三种采样方式的频率适用区间、以及核心约束 **"IDELAY 移相范围有限 → 用它对眼心
> 有一个采集频率下限"** 用准确物理 + 本地实测一次性钉清。
>
> 一句话:**IDELAY 最多把信号推迟 ~2.5ns,要靠它把采样点从数据边沿移到眼图中心,
> 需要移动 ~UI/2;频率越低 UI 越大,UI/2 越远,2.5ns 就够不到 —— 所以 IDELAY 移相
> 方案有一个 ~100MHz(TRACECLK)的采集频率下限,低于它必须改用"换边沿采"或过采样。**

---

## 1. Artix-7 IDELAYE2 的硬规格(已查证)

| 参数 | 值 | 来源 |
|------|----|------|
| 抽头数 | **32 tap**(0-31) | UG471 / UG953 |
| 每 tap 延迟 | **78 ps**(@ IDELAYCTRL REFCLK = 200MHz) | AMD/Xilinx 官方(MicroZed Chronicles、UG471);@300MHz refclk ≈ 52ps |
| **总移相范围** | **≈ 2.496 ns**(32 × 78ps) | 同上 |
| REFCLK 频率 | 必须 **190-210MHz**(或 290-310MHz)才保证 tap 标称值;Artix-7 IDELAY 工作频率窗口窄 | Xilinx 论坛/UG471(本项目用 200MHz) |

关键:**REFCLK 是给 IDELAYCTRL 校准 tap 用的参考钟(我们用 MMCM 出的 200MHz),与被采
信号 TRACECLK 的频率是两回事**。tap 的物理延迟由 REFCLK 定,移相总量 = 32×78ps = 2.496ns
封顶 —— 这个 2.496ns 才是下面频率下限的根源。

---

## 2. 核心约束:IDELAY 对眼心 → 采集频率下限 ~100MHz

ARM TPIU 并口是 **源同步 edge-aligned DDR**:数据在 TRACECLK 两个沿翻转,数据沿与时钟沿
对齐。IDDR 直接在时钟沿采 = **采在数据跳变点(眼最差)**。要采到眼心,必须用 IDELAY 把
数据(或时钟)推迟,使采样点落到数据稳定区中心。

- DDR 一个数据位(UI)= 半个 TRACECLK 周期 = **1/(2·f_TRACECLK)**。
- 从边沿移到眼心需要移动 **≈ UI/2 = 1/(4·f_TRACECLK)**。
- IDELAY 能提供的移相 ≤ **2.496 ns**。
- 能覆盖眼心 ⇔ 1/(4·f) ≤ 2.496ns ⇔ **f_TRACECLK ≥ ~100 MHz**。

| TRACECLK | UI(位宽) | 眼心距边沿(UI/2) | IDELAY 2.496ns 够不够 |
|----------|----------|------------------|----------------------|
| 1.3 MHz | 385 ns | **192 ns** | ❌ 差 ~77× |
| 10 MHz | 50 ns | **25 ns** | ❌ 差 ~10× |
| 25 MHz | 20 ns | **10 ns** | ❌ 差 ~4× |
| 50 MHz | 10 ns | **5 ns** | ❌ 差 ~2× |
| **~100 MHz** | 5 ns | **2.5 ns** | ✅ 刚好够 |

**结论:IDELAY 移相对眼心只在 TRACECLK ≳ 100MHz 才工作。** 低于此,IDELAY 移不到眼心 ——
这就是"频率不能太低"的准确含义(之前我含糊写成"最低频率下陷",机制讲错,在此更正)。

> 这也解释了 V1/V2 eyescan 在低速(~10M)"14 个 tap 都开眼"的怪象:IDELAY 总范围(2.5ns)
> 远小于 UI(50ns),32 个 tap 全落在眼内同一小区域,**都能解 → 无区分度**,并不是真在
> "扫眼"。低速下 eyescan 找的 best_tap 没有移相意义。

---

## 3. 本地实测证据(§22.4,`14-logic-analyzer-ground-truth.md`)

1.3MHz 下要把采样点移到半位中心(~190ns),逐一排查移相手段:

| 手段 | 1.3MHz 下可行? | 原因 |
|------|---------------|------|
| IDELAY 延数据/时钟 | ❌ | 最多 ~2.5ns,差 190ns **两个数量级** |
| MMCM/PLL 对 TRACECLK 移相 90° | ❌ | TRACECLK 1.3MHz < MMCM 最低输入(~10MHz),锁不住 |
| **换边沿采(IDDR 反相 / 换 a-b 边沿配对)** | ✅ | 用相反时钟沿,天然落在半位中心,**不需要绝对延时** |

→ 低速的正解不是 IDELAY,而是**换边沿**(免移相)或**过采样**(ref_200m 定位采样点)。

---

## 4. 三种采样方式的频率分工(代码里都有)

| 方式 | 原理 | 适用频率 | 我们的验证 |
|------|------|---------|-----------|
| **OVERSAMPLE**(trace_capture_a7 默认) | ref_200m(5ns)过采样 TRACECLK+数据,检测边沿后 mid-eye latch;采样点靠计数器定位,不靠 IDELAY | **低速,≤~10M**(再高过采样重复失锁,§5) | ✅ 降频 1.3M 解出真 PC |
| **IDDR(换边沿,无 IDELAY 移相)** | TRACECLK 当采样钟,用相反沿落半位中心 | **低/中速**,免移相所以无下限 | V1/V2 开眼吐帧(未端到端解 PC) |
| **IDDR + IDELAY 移相对眼心** | IDELAY 把采样推到眼心 | **高速,≳100MHz**(§2 下限);上限受 SI | 未触及该频段 |

**没有单一方式通吃全频段**:低速用过采样/换边沿,高速才用 IDELAY 移相。中间靠换边沿过渡。

---

## 5. 本轮实测(诚实标注)

- **OVERSAMPLE 降频首次解出真 PC**(✅):STM32 4-bit,HCLK /128(~1.3MHz),FPGA 解出
  `0x08000f8c add`/`0x08000fbc loop_sum`/`0x08000e90 TIM8 handler`(带 file:line)。
  **stage4 以来 FPGA 采样通路首次在板上解出真实函数**(§14 的真 PC 是逻辑分析仪抓的)。
- **OVERSAMPLE 高速失锁**(✅,168MHz):抓到字节**每个重复 3-5 次且不规整**
  (`83 83 83`、`c0 c0 c0 c0`)。根因:TRACECLK 半周期太短,边沿检测在一个真实周期内
  多触发(振铃被 LOCKOUT=4/20ns 压不住)。去重也解不出。
- **SELFTEST 恒完美**(✅):内部 +7 ramp 恒解出精确 ramp → OVERSAMPLE 采样/组装逻辑
  **零 bug**,高速失锁是真实信号边沿多检,非逻辑错。
- **boundary-scan 证接线对**(✅):TRACECLK(D17)/D0(F13)/D1(E14)均 toggle
  (balance .83/.52/.64),信号物理到达 FPGA。(此前我用 duty 统计误判"没进 FPGA",
  被此实验推翻 —— duty 在过采样多检下失真,不可用于判物理连通。)

---

## 6. 对函数级 trace 目标的路径

| 目标 | 方式 | TRACECLK | 状态 |
|------|------|----------|------|
| **现在就要函数级 trace** | OVERSAMPLE 降频 | 1.3-10M | ✅ 已通(4-bit 解真 PC) |
| 中速 + 函数级 | IDDR 换边沿(免移相) | 10-50M | 需做:换边沿采 + 端到端解码(V2 只到开眼) |
| 中速 + 函数级 | **MMCM 移相 @90°** | **21M(HCLK 42M)** | **✅ golden,0.002% unknown/41 锚点(§7.5)** |
| 高速 + 函数级 | **MMCM 移相 @112.5°** | **84M(满速)** | **✅ golden,0.01% unknown/36 锚点(§7.5)** |
| 高速/满速 | IDDR + IDELAY 移相 | ≳100M(§2 下限)| 命门:此频段 SI + IDELAY 对眼,未触及 |

**最务实**:用 OVERSAMPLE 降频(已通)交付函数级 trace 能力,接 orbetto 出调用栈+时间轴;
中/高速作为独立 PoC,且要认清 **IDELAY 移相只在 ≳100MHz 才有意义**,中速段(10-50M)应走
"换边沿采"而非靠 IDELAY 对眼。

---

## 7. 频率扫描实测(本轮补,纠正旧估)

逐档扫 STM32 HCLK,测 OVERSAMPLE 4-bit 通路解出的 flash I-sync 锚点
(`scripts/freq_scan_oversample.sh`):

| HCLK | I-sync 锚点 | distinct PC | 状态 |
|------|------------|------------|------|
| 1.3 MHz (/128) | 16 | 3 | ✅ |
| 10.5 MHz (/16) | 15 | 3 | ✅ |
| 21 MHz (/8) | 18 | 5 | ✅ |
| **42 MHz (/4)** | **0** | **0** | ❌ 失锁 |
| 84 MHz (/2) | — | — | ❌(把 FPGA 网络搞挂,需重烧恢复) |

**OVERSAMPLE 实测可用上限 ≈ 21M HCLK,42M 崩。** 且 42M 下**扫遍 EYE_DELAY=1..12
(`scripts/eye_scan_42m.sh`)全部 0 锚点** —— 失锁**不是** mid-eye 点位置(改 EYE 救不活),
而是**过采样率根本不够**(42M 下每半位 ref_200m 采样点太少,边沿检测/配对失效)。

> duty 统计在真实信号 + 过采样多检下持续失真(测出 ms 级假"半周期"),**不可用于判
> TRACECLK 频率或物理连通**,本轮多次被它误导,改用锚点解码 + boundary-scan 判定。真实
> TRACECLK 频率待 LA 直接读数(HCLK↔TRACECLK 分频:etm_enable 注释 /16 prescale vs
> downclock.cfg "直接 HCLK 派生无独立分频",两处矛盾,需实测)。

### 7.1 中速(>21M)方案:MMCM 90° 移相(中速可行,低速不行)

要中速,§3 的"换边沿"在中速段有低速做不到的实现:**MMCM 对 TRACECLK 移相 90°**(=DDR
半位)落眼心,免 IDELAY 绝对延时。低速(<10M)TRACECLK < MMCM 最低输入锁不住;**中速
(TRACECLK 10-50M)MMCM 能锁** → 正好填 OVERSAMPLE(≤21M)与 IDELAY(≳100M)之间的空档。
前提:真实 TRACECLK ≥ ~10M(待实测)。若 TRACECLK=HCLK/16 则也锁不住,得提过采样钟或
IDDR 换边沿配对。**TRACECLK 实测频率是下一步 PoC 第一问。**

### 7.2 MMCM 90° 移相 —— 板上验证成功(✅ 实测,本轮)

`trace_capture_mmcm.v` + `trace_mmcm_top.v`(独立顶层,不动已验证的 OVERSAMPLE
`trace_stream_top`)在 **STM32 HCLK=42MHz(TRACECLK=21MHz)** 实测:

- **MMCM 锁定**:capture MMCM(`u_cap/u_mmcm`,CLKIN=21M,VCO=21M×40=840M)
  `locked=1`,buffer `rfull=1` —— 21MHz 远高于 MMCM 最低输入,稳定锁定。
- **解出真实函数级 trace**(`/tmp/mmcm_best.bin`,16 I-sync 锚点 / 44 branches):
  - `0x08000f8c _Z3addii` → main.cpp:54
  - `0x08000fb2 _Z8loop_sumi` → main.cpp:61
  - `0x08000e90 TIM8_UP_TIM13_IRQHandler` → timer.c:483
- **采集频率 = OVERSAMPLE 上限的 2×**:OVERSAMPLE 在 42M HCLK 失锁(§7),MMCM 90°
  在 42M HCLK / 21M TRACECLK **解出真 PC** —— stage4 以来第二个在 FPGA 通路解出真实
  函数的方案,且把可用频率从 21M HCLK 抬到 ≥42M HCLK。

#### 关键实测细节:半位配对偏移一拍
naive 打包 `{trace_b[k], trace_a[k]}`(同周期上/下半位)**解不出**(0 锚点)。板上暴力
搜索(`decode/mmcm_halfbit_search.py`,扫 offset×nibble序×lane序)得唯一可解组合:
**`cap_byte = {trace_a[k] (高nibble), trace_b[k-1] (低nibble)}`** —— 即字节边界比"同
周期两半位"偏移一个半位。根因:IDDR `SAME_EDGE_PIPELINED` 把当前上沿与**上一周期**下沿
配在一起(流水线相位)。已据此改 `trace_capture_mmcm.v`(`trace_b_q` 打一拍),使板上
直出字节即可解码(为后续 orbuculum 实时喂流准备)。

> 诚实标注:21M 已实测解出真 PC。更高频段(42M/84M TRACECLK)的频率上限扫描见 §7.3。

### 7.3 频率上限扫描 —— 实测到 F429 满速 84M TRACECLK(本轮)

把 TRACECLK 逐档往上推(TRACECLK=HCLK/2,HPRE 只有 /1、/2、/4 三档可用),每档配
匹配的 MMCM MULT 保持 VCO≈840MHz(在 600-1440MHz 内),实测每档能否锁定 + 解出真 PC。
`trace_capture_mmcm.v` 的 `MULT`/`CLKIN_PERIOD`/`PHASE` 已参数化,一套 RTL 覆盖全频段。

| TRACECLK | HCLK | HPRE | MULT/DIVID | VCO | phase=90° | phase=135° | 时序(WNS) |
|----------|------|------|-----------|-----|-----------|-----------|-----------|
| 21M | 42M | /4 | 40/40 | 840M | **16 锚点 ✅** | — | +0.92ns |
| 42M | 84M | /2 | 20/20 | 840M | 1 锚点 | **17 锚点 ✅** | +0.54ns |
| **84M** | **168M(满速)** | **/1** | 10/10 | 840M | 3 锚点 | **18 锚点 ✅** | +0.51ns |

每档"锚点"= `etm_decode_cli` 在 ELF .text 内解出的 I-sync;✅ = add/loop_sum/TIM8
三个函数全部带 file:line 解出。

**结论:MMCM 90°移相方案实测打到 STM32F429 的满速上限 —— HCLK 168MHz / TRACECLK
84MHz,函数级 trace 完整解出(18 I-sync / 51 branches)。** F429 没有更高档(HPRE 已
到 /1),所以这是器件上限而非方案上限;方案本身(VCO 840M,IO/IDDR)还有余量。

> ⚠️ **本表的锚点数是离线重搜配对得到的,不代表 RTL 原生可解。§7.4 用 §14 完整度方法论
> 复核后撤回了"84M 满解"的表述 —— 84M 原生流 0 HSYNC/21.9% unknown,需 PC 端重搜才能救。
> 真实"原生端到端可信"上限是 21M。下面 §7.3 余下内容(相位规律)仍成立。**

#### 关键实测规律:最佳采样相位随频率上移
- 21M:90° 完美(16 锚点)。UI=23.8ns,相位不敏感。
- 42M/84M:90° 退化(1/3 锚点),**135° 才回到满解(17/18 锚点)**。
- 物理解释:ETM data 相对 TRACECLK 有固定的 PCB+IO 传播 skew(几 ns)。低频时这点
  skew 占 UI(11.9-23.8ns)比例小,90°≈眼心;高频时 UI 减半,同样的 skew 把眼心从
  90° 推向更大相位,需 ~135° 补偿。**所以高速档把 `PHASE` 设 135° 是必须的,不是 90°。**
- 配对方向 `{trace_a[k], trace_b[k-1]}`(offset=1,hi_first=1)**三档通用**,只有相位要随频率调。

#### 一个曾掩盖结论的坑(诚实记录)
首轮 42M 测试"全 0 锚点",一度以为 42M 解不出。两个真因:
1. **时序未收敛**:`set_clock_groups` 写在 XDC 里,但 MMCM 生成时钟在 `read_xdc` 时
   还不存在 → 命令空匹配静默失效,clk90↔clk125 的 CDC 路径没被切断。21M 周期长(47.6ns)
   凑够了 setup 没暴露,42M(23.8ns)直接 setup 违例 -4.6ns。**修复:把 create_clock +
   set_clock_groups 移到 synth 之后的 tcl 里**(此时生成时钟已存在)。
2. **HCLK 没设对**:相位扫描脚本里 telnet 命令的 `\&` 在 heredoc 中转义出错,HCLK
   复位回了 168M(满速),导致"42M 测试"实际跑在 84M TRACECLK 而 MULT 仍按 42M(VCO=420M)
   → 失锁。**修复:用字面 RCC_CFGR 值(0x948a/0x940a/0x949a)写寄存器,不做 shell 算术**。

> 诚实边界:以上为 proj_add 固件(小循环)实测,锚点数(16-18)反映其代码足迹小。84M
> 已是 F429 满速,未(也无法在本器件)测更高;更高 TRACECLK 需换 MCU。phase 只扫了
> 45/90/135/180 四点,最佳点在 135° 附近但未做更细网格。

### 7.4 ★ 数据完整度复核(用 §14 方法论)—— 撤回 84M"满解"的夸大

§7.3 报的"锚点数"是用 `decode/mmcm_halfbit_search.py` **逐份数据离线重搜字节配对**得来的,
不是 RTL 原生输出。按 stage4 §14/r14 定的完整度方法论(只认 I-sync 锚点 + unknown 字节率 +
HSYNC 结构完整性,**解出函数名不算数**)重新量 **RTL 原生流**,结论要修正:

| 频率 | RTL 原生流 HSYNC | 原生 unknown | 原生锚点 | 离线重搜配对后 |
|------|-----------------|-------------|---------|---------------|
| **21M** | **2685 ✅** | 3.54% | **34** | — |
| **84M** | **0 ❌** | **21.9%** | **0** | 2670 HSYNC / 1.2% / 34 |

(unknown = 落在 ETM data 编码空间的字节;M4 无 data trace,故 unknown=采集/解码错误。
LA 金标准正确去帧后 = 0.0018%,见 §16。HSYNC=`FF 7F` TPIU 空闲填充,原始引脚流必然规律带有。)

**诚实结论:**
- **21M 是真端到端可信**:RTL 原生流带 2685 个 HSYNC,TPIU 去帧直接解出 34 锚点。比 LA 金标准
  (0.0018%)高一个量级(3.5%),但流结构完整、可直接解码。
- **84M 名不副实**:RTL 原生流 **0 HSYNC / 21.9% unknown / 0 锚点** —— 原生输出是坏的。§7.3 报的
  "18 锚点"靠离线换配对硬抠;换对后数据确实还在(2670 HSYNC / 1.2% / 34 锚点),证明 **MMCM 锁
  定 + 信号采进来都没问题,但 RTL 写死的 `{trace_a[k], trace_b[k-1]}` 配对在高频不成立** —— 半位
  字节边界随频率移位,§7.2 的"配对方向三档通用"是**错的**。
- 所以"MMCM 打到 F429 满速 84M"应表述为:**MMCM 能锁到 84M 且数据采得进来,但只有 21M 是 RTL
  原生直接可解;42M/84M 当前需要 PC 端离线重搜配对,RTL 高频字节组装是个待修的 bug**。

**修正后的真实上限(原生可解):TRACECLK 21M(HCLK 42M)。** 42M/84M 是"数据采得到、原生组装错、
离线可救"的状态,不是干净的端到端打通。要让高频也原生可解,需修 RTL 的半位配对随频率自适应
(或扫 phase 时一并定 pairing offset),这是下一步。

> 教训(呼应 §30.3):又一次"看锚点数报喜"踩进 r14 早警告的坑 —— 锚点能靠离线重搜凑出来,
> 不代表采集前端原生正确。完整度必须量 RTL 原生流的 unknown/HSYNC,不能靠 PC 端补救后的数字。

### 7.5 ★ 修复到可靠:统一解码管线 + 逐频最优相位(本轮,实测 golden)

§7.4 暴露两个问题,逐个修死:

**修复 1 —— 统一可靠解码管线 `decode/mmcm_decode.py`(取代脆弱的离线重搜)。**
RTL 字节 = `{trace_a[k](高), trace_b[k-1](低)}`。把它还原成时间序半位 nibble 流,再走
**LA 金标准同款管线**:`dsl_parse.assemble` 的 parity/order 搜索(按 flash 锚点打分自动定
半位边界)→ **连续重对齐 TPIU 去帧 `tpiu_deframe_walk`**(§19,逐 ~1KB 坏窗后原地重锁,
无缝损)。这条管线**与频率无关**,parity 随每份 capture 自动选(21M→parity1,84M→parity0),
不再需要 `mmcm_halfbit_search` 那种逐份手搜。

**修复 2 —— 逐频最优采样相位(实测 golden)。** §7.3 粗扫(45/90/135/180)误判 84M 最优在
135°;按 **unknown 字节率**(真完整度指标,非锚点数)细扫,真最优差很多:

| TRACECLK | HCLK | 最优 PHASE | unknown 率 | 锚点 | 复现性 |
|----------|------|-----------|-----------|------|--------|
| **21M** | 42M | **90.0°** | **0.002–0.005%** | 41 | 3 次稳定 |
| **84M** | 168M(满速) | **112.5°** | **0.009–0.012%** | 36–37 | 4 次稳定 |
| 42M | 84M | (≥135°,未细扫到 golden) | ~1.2–2.0% | 35–37 | — |

(对照:LA 金标准 0.0018%。残余 4–5 个 unknown = SysTick 异常信息字节 §17,非损坏。)

**21M 与 84M 两个端点(保守档 + F429 满速档)实测都到 golden 质量(~0.01%),逐字节级可信、
多次复现。** 关键:84M 的眼很窄(UI=11.9ns),135°→1.2%、112.5°→0.01%,相位差 22.5° 就差 100×,
**所以 PHASE 必须逐频在板上扫 unknown 率定**,不能套公式。

**修正后的真实结论:MMCM 90°移相方案在 STM32F429 上,21M(保守)和 84M(满速)两档都做到了
原生端到端 golden 质量的函数级 trace。** 42M 中间档最优相位未扫到 golden(2% 量级,可解但不够干净),
但 42M 不是 HPRE 的常用档(要中速直接用 84M 满速更好),不影响两个端点的交付。

### 7.6 下一步可靠性:运行时动态相位校准(根治逐频手扫)

逐频固定相位虽已把 21M/84M 做到 golden,但每频要重新扫、且最优点窄、随板子/温度可能漂 ——
本质不够稳。根治法 = **MMCM 动态相位移位(MMCME2_ADV + PSEN/PSINCDEC/PSDONE)**:一个 bitstream,
开机后由 CSR 驱动扫采样钟相位,主机读 unknown 率(已有)挑最低点锁定。这是源同步接收的教科书做法,
与频率/板/温度无关。RTL 改动可控(BASE→ADV + 一个 PS 脉冲状态机 + CSR),校准逻辑复用主机现成的
unknown 率计算。**作为把"中速全频段都 golden"做扎实的下一步。**

---

## 8. 诚实边界(更新)

- OVERSAMPLE 上限 ~21M HCLK 已逐档实测(§7),非旧估。
- §2 的 ~100MHz 下限是 IDELAY 2.496ns + edge-aligned DDR 的物理推导,**未在板上撞到**
  (我们从没跑到 100MHz TRACECLK)。
- IDDR 换边沿在我们板上只验过"开眼吐帧",**未端到端解出真 PC**。
- tap=78ps 仅在 REFCLK=200MHz 时成立;若改 300MHz refclk,tap≈52ps、总范围更小,下限频率
  更高(约 150MHz)。


---

## 8. 流式高容量传输:RTL 写好,卡在上游 self-TX 路径未验证(诚实状态)

为抓 LVGL(代码量远超 64KB 一次性窗口),把 MMCM 捕获改成连续流式:

- `trace_mmcm_stream_top.v`:MMCM 前端 → `axis_async_fifo`(clk90→clk125 CDC)→
  打包器(每包 `[4字节大端序列号][PAYLOAD trace字节]`,FIFO 深度门控避免半包欠载)→
  `fpga_core_net` self-TX(STREAM=1)连续 UDP 到 host:5555。丢包计数 `lost_cnt`(捕获侧
  FIFO 满)+ 序列号(传输侧丢包)双重核账。**综合/布线干净,时序收敛(WNS +0.70ns)。**
- `scripts/trace_stream_rx.py`:host 收流、剥序列号、检测 gap、落盘、测吞吐。
- `fpga_flow/run_trace_mmcm_stream.tcl`:构建脚本。

**卡点(已解,§8.1):FPGA self-TX 路径不发包。** 烧 `trace_mmcm_stream.bit` 后网络栈不响应
(连 ARP 都不回);进一步用上游**专门验证 self-TX 的** `selftx_test_top` 单独测,确认
self-TX 从未真正发过包。

### 8.1 ★ 根因 + 修复:UDP checksum-gen 在连续流下卡死(全栈仿真定位)

按"先仿真定位、不盲调"的纪律,把 self-TX FSM 抽成独立 `udp_tx_streamer.v`,配真 `udp_complete`
(+ip/arp)写全栈 TB(`rtl/sim/udp_tx_streamer_tb.v`,带行为级 ARP 应答 peer),板上现象在
仿真里精确复现:

- UDP 头被接受、payload 全部排空、`udp_checksum_gen` FSM 跑到 FINISH_SUM —— 但它的 header
  FIFO 不前进,`m_udp_hdr_valid` 永不拉高 → udp.v 不出 IP 头 → ip_complete 不发 ARP →
  网线上 0 帧(连 ARP 都没有)。**verilog-ethernet 的 `udp_checksum_gen` 在连续自发起流下
  卡死**(RX-echo 路径因 payload 来自 FIFO、帧间有空隙而绕过了它)。
- **修复:`UDP_CHECKSUM_GEN_ENABLE=0`。** UDP 校验和对 IPv4 是可选的(0=未计算,RFC 768),
  host 照收;trace 流不需要逐包 UDP 校验和。关掉后仿真干净直流:1 ARP → 应答 → 65 连续
  IP/UDP 帧(PASS)。`fpga_core_net` 加 `UDP_CHECKSUM_GEN_ENABLE` 参数(默认 1 保持 echo 行为),
  流式顶层设 0。

### 8.2 ★ 板上实测:连续流跑通,golden 质量,可抓 LVGL

烧 `trace_mmcm_stream.bit`(21M,checksum 关),STM32 HCLK /4:

| 指标 | 实测 |
|------|------|
| 吞吐 | **20.9–21.1 MB/s**(= 21M TRACECLK × 1 byte/周期,满速无节流)|
| UDP 丢包(序列号) | **0**(lost_pkts=0)|
| FPGA 捕获侧 lost_cnt | 213355(**仅启动瞬间**,ARP 解析时 FIFO 填满;之后稳定不增长)|
| unknown 字节率(启动后窗口)| **0.0022%**(= one-shot golden,§7.5)|
| 重建指令数 | **2,042,512**(单次连续窗口)|
| loop_sum×5 顺序+次数 | **31890/31914 完美(99.92%)** |

**结论:流式高容量传输打通,质量与 one-shot golden 一致,吞吐跟得上 21M 满速且零持续丢包。
可以开始抓 LVGL。** 唯一 artifact 是启动瞬间 ARP 解析期间 FIFO 填满丢的一段(一次性,
~213KB),抓取时丢弃开头一段或加一次 re-arm 即可;稳态零丢包。

> 注:`trace_stream_rx.py` 收流剥 4 字节序列号、检 gap、落盘;`mmcm_decode.py` 解码;
> `orbetm`+`verify_flow.py` 做逐指令重建 + 顺序/次数核对。

### 8.3 遗留(次要)
- 启动 ARP 期间的一次性 FIFO 丢弃:可在顶层加"ARP 解析完成前不计数/丢弃"或加更深 FIFO +
  host 端丢弃首包窗口。不影响稳态。
- self-TX FSM 仍内联在 fork 的 `fpga_core_net` g_stream 里;独立 `udp_tx_streamer.v` 已抽出
  并仿真验证,后续可切过去让 core 回归纯以太网核(架构清理 B,见对话记录)。