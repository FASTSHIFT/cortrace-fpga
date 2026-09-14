# Stage-4 · 频率扫描与采样策略测试方案

> 目标:在低速 100% 正确(§30,unknown ~0.003%)基础上,系统测出本采集系统的**频率承压上限**,
> 并确定不同频率档位的采样策略切换点。原则:**动态 + 批量 + 多次重复,尽量零重新编译烧录。**

## 0. 前置:任务 1(读出 off-by-one)RTL 修正已并入

`fpga_core_net.v` 的 `ext_pos` 提前 1 拍补偿 BRAM 读延迟;trace_dump 恢复朴素分页读。用 SELFTEST ramp
ground truth 验证读出干净后,频率扫描的所有误码数才可信(否则又是测量假象)。

## 1. 频率怎么变(零烧录)

TRACECLK = STM32 HCLK,由 `downclock.cfg` 改 `RCC_CFGR.HPRE` 分频决定,**与 FPGA 位流无关**:

| DIV | HPRE | HCLK≈ | TRACECLK(DDR 半位) | 备注 |
|-----|------|-------|-----|------|
| /512 | 0xF | 0.33 MHz | 1.5 µs | 极慢,基线 |
| /256 | 0xE | 0.66 MHz | 760 ns | |
| /128 | 0xD | 1.3 MHz | 380 ns | |
| /64 | 0xC | 2.6 MHz | 190 ns | **当前 100% 基线** |
| /16 | 0xB | 10.5 MHz | 48 ns | |
| /8 | 0xA | 21 MHz | 24 ns | 过采开始吃力区 |
| /4 | 0x9 | 42 MHz | 12 ns | |
| /2 | 0x8 | 84 MHz | 6 ns | ref=200M 仅 1.2 拍/半位,过采失效 |
| /1 | 0x0 | 168 MHz | 3 ns | 必须 IDDR |

> 注:HCLK 实际值取决于固件 SystemClock_Config 设的 PLL;上表按 168MHz 系统时钟估算,实测以读 RCC 为准。
> 改频率只需跑一条 OpenOCD(`DIV=n ... downclock.cfg`),不 reset、不重烧 FPGA。

## 2. 采样策略随频率分三档(关键)

| 档 | 条件(半位 vs ref 周期 5ns) | 策略 | RTL |
|----|------|------|-----|
| 低频 | 半位 ≫ 5ns(≥ ~40ns,DIV≥/16) | **OVERSAMPLE**,EYE_DELAY 落眼内即可 | 现有 |
| 中频 | 半位 ~ 15–40ns(DIV /8–/4) | OVERSAMPLE,**EYE_DELAY 必须 = 半位/2 自适应**;边沿检测 ±1 拍抖动开始占比变大 | 现有 + 自适应 EYE |
| 高频 | 半位 < ~3 个 ref 周期(DIV≤/2) | 过采失效 → **IDDR + IDELAY per-lane deskew**(TRACECLK 当采样时钟) | 需另写(§5) |

**本次扫描的核心产出 = 实测出 OVERSAMPLE 的频率上限,以及必须切 IDDR 的临界点。**

## 3. 零烧录的可调旋钮设计

为了"一次烧录扫完整个矩阵",把编译期参数改成**运行时 CSR**(通过 UDP 控制端口写):

- **EYE_DELAY** → 运行时寄存器(替代 `EYE` generic)。一次烧录即可扫所有 EYE 值。
- **(可选)CAP_METHOD** → 若想在同一位流里切 OVERSAMPLE/IDDR,做成运行时 mux + 两条采集路径并存,
  由 CSR 选。代价是面积翻倍但省去重烧。先不做,IDDR 档单独烧一次。

控制通道:复用现有 UDP readout 框架,新增一个写寄存器端口(如 :5002),payload = {reg_addr, value}。

## 4. 测试编排(软件循环,批量 + 重复)

一次烧录后,PC 端脚本 `freq_sweep.py` 跑双重循环:

```
for DIV in [512,256,128,64,16,8,4,2,1]:
    openocd 设 HPRE=DIV               # 改频率,不 reset
    for EYE in [auto, 一组候选]:
        写 EYE CSR                     # 运行时设采样点
        repeat R 次:
            重 arm FPGA(reload bit 或 soft-rearm CSR)
            trace_dump
            fpga_errrate → 记录 unknown%、锚点数、杂散PC数
    汇总该 DIV 的 中位数/最差/方差
输出:误码-频率曲线 + 每频率最优 EYE + OVERSAMPLE 上限拐点
```

重复 R 次(如 10)取统计,排除单次偶发;记录 median 和 worst-case。

**soft re-arm**:目前 re-arm 靠重烧位流(慢)。应加一个 CSR 复位 capture FSM 的位,让 re-arm = 写寄存器
(快、可批量)。这是省时间的关键改动。

## 5. 高频档(IDDR)预案

当 OVERSAMPLE 在某频率开始劣化,切 IDDR 路线:
- TRACECLK 经 BUFG/BUFR 当采样时钟,IDDR 双沿采;
- IDELAYE2 per-lane 扫 tap 找眼心(eye-scan 训练,FPGA 内做或 PC 辅助);
- 这是源同步标准做法,高频下 IDELAY 的 ~2.5ns 范围相对几 ns 的半位足够。
- 单独烧一个 IDDR 位流,跑同样的 freq_sweep 对比。

## 6. 验收指标

- 每个 (DIV, EYE, 重复) 点:unknown%、flash 锚点数、杂散 bit-flip PC 数。
- **频率上限定义**:unknown 持续 < 0.1%(或锚点稳定全中)的最高 TRACECLK。
- 产出曲线:unknown% vs TRACECLK(每档最优 EYE),标出 OVERSAMPLE→IDDR 切换点。
- SELFTEST(固定内部频)全程当常驻探针:任一频率出问题时,先跑 SELFTEST 确认采集链+读出仍干净,
  从而把"物理层/频率相关"与"采集链 bug"分开(避免再被测量假象误导)。
```


## 7. 实测进展(运行时 CSR 已通,频率标定存疑)

### 7.1 已完成

- **运行时 CSR 打通**:UDP :5002 写 EYE(0x01)/ 软 re-arm(0x02);`trace_ctrl.py` + `freq_sweep.py`。
  一次烧录、零 reflash 跑 DIV×EYE 矩阵已验证可用。
- **读出 off-by-one 终修**:RTL ext_pos 提前一拍的尝试在真实读出下**不可靠**(首拍 AXI stall,SELFTEST
  ramp 看着干净但真实流仍有每页重复字节)。改回 trace_dump 端**每页多读 1 字节丢首字节**的确定性修法,
  实测真实 trace **0.000% unknown**。教训:别用一个数据集(ramp)的干净就推断修复对所有数据成立。
- **板上验证**:CSR set-eye + 软 re-arm + drop-first 读出 = unknown 0.000%、38 锚点、0 杂散,可复现。

### 7.2 存疑:DIV 是否真的改变了 TRACECLK(必须用 LA/示波器证实)

freq_sweep 在 DIV=/64../1 全部得到 **0.000% unknown + 完全相同的 38 锚点**。这有两种可能:
1. 过采样在所有这些频率下都完美(乐观);
2. **DIV 实际没怎么改变 TRACECLK 速率**(存疑)。

旁证指向需要警惕:
- 锚点数在所有 DIV 完全相同(38)——锚点由 ETM 1024 字节同步周期驱动,是字节域量,**与时钟速率无关**,
  所以"锚点相同"既不能证明也不能否定速率变了。
- 用"buffer 填满时间"当速率探针**失败**:/512 和 /1 填充都是几百 ms 级缓慢。原因:tight while 循环里
  ETM 只在分支/同步时**稀疏突发**地出 trace,填充时间被**trace 数据产生率**门控,不是 TRACECLK 速率。
- RCC_CFGR 确实被写入(HPRE 字段在变),但 F429 并口 TRACECLK 与 HCLK 的实际关系、以及 TPIU 是否对并口
  也有自己的分频,**没有用 LA/示波器实测确认过**。

**结论(诚实标注)**:字节域测量**在结构上无法**给出绝对 TRACECLK 频率。"0% 跨所有 DIV"是真实的解码质量
结果,但**不能据此声称达到了某个频率上限**。要标定频率,必须:
- 用 LA(50MSa/s)直接量不同 DIV 下 TRACECLK 的周期,或
- 跑一段已知指令数/已知时长的程序,用 trace 的时间戳/字节量反推速率。

### 7.3 下一步

1. 用 LA 实测 DIV=/64 vs /16 vs /1 下 TRACECLK 的真实周期(几分钟,直接定标)。
2. 标定后,才能在"已知真实频率"轴上画 unknown%-频率曲线、找过采样上限、确定 IDDR 切换点。
3. 在那之前不对"频率承压上限"下任何结论。


## 8. 过采样频率上限分析(从 RTL 算法约束推导)

实标定:SYSCLK=168MHz(实读 RCC_PLLCFGR=0x07405408:HSE8/M8×N336/P2),TRACECLK = HCLK/2。
当前 `ref_200m` = 200MHz(5ns/拍)。当前算法:TRACECLK 边沿后等 EYE_DELAY 拍 latch,每边沿产一 nibble。

约束逐条(f = TRACECLK,半位 = 1/(2f)):

| 约束 | 每半位需 ref 周期数 | 半位下限 | TRACECLK 上限 |
|------|------|------|------|
| 边沿可分辨(2-FF 边沿检测,绝对极限) | 2 | 10ns | 50MHz(危险) |
| 眼内采样 + 容 ±1 拍抖动(**实际可用**) | 4 | 20ns | **25MHz** |
| 远离跳变、舒适裕量(**已验证安全**) | 8 | 40ns | **12.5MHz** |

**结论(ref=200MHz 当前实现)**:
- 舒适可靠 ≤ **12.5MHz** TRACECLK(覆盖 DIV/16=5.25MHz、/8=10.5MHz)。
- 可用有余量 ≤ **25MHz**(DIV/4=21MHz 贴边,需实测确认)。
- 50MHz 理论极限,不可靠。
- DIV/1 = TRACECLK **84MHz** → 半位 6ns,每半位仅 ~1.2 个 ref 点,**过采样物理上不可能工作**。
  → 之前 freq_sweep 在 /1 报 0.000% + 38 个完全相同锚点 = **soft re-arm 高速竞态读到旧 buffer 的假象**,
    不是真采到 84MHz。(又一例字节域指标骗人,被 LA 抓不上当场戳破。)

**上限正比于 ref 频率**。要往高推:
1. 提 ref 过采时钟(MMCM 出 400MHz → 上限翻倍);再高用 ISERDESE2 1:4/1:8 串并等效过采到 ~GHz,
   把过采样路线推到 100MHz+ TRACECLK。
2. >25MHz 切源同步 IDDR + per-lane IDELAY 扫眼(业内高速标准;此时半位几 ns,IDELAY 的 ~2.5ns 够用——
   低频反而不够,所以低频必须过采样)。

**待实测**:在 LA 可覆盖区(/64=1.31MHz、/16=5.25MHz,可能 /8=10.5MHz)做 LA+FPGA 对拍标定,验证
12.5MHz 舒适线;并先修 soft re-arm 高速竞态(加"新捕获已写入"确认),否则高频测量继续被旧 buffer 污染。


## 9. 舒适区实测(gen 确认 + worst-case 跟踪)——发现间歇性损坏

### 9.1 新增可信度机制

- **捕获代数计数器(gen)**:每次 soft re-arm 递增,暴露在状态字节 NB+3。trace_dump `--prev-gen`
  轮询直到 gen 变化且 full=1,**确认读到的是新捕获**(根除"读到旧 buffer"的假象,这是 /1 那个 0% 假象的根源)。
- 状态区(NB+0..3)是组合 mux,无 BRAM 读延迟,故**不**需要丢首字节(数据区才需要)。

### 9.2 实测结论:存在间歇性区域损坏(EYE 无法消除)

DIV/64(TRACECLK 1.31MHz)EYE 细扫 + worst-case:

| EYE | unk% 中位 | unk% 最差 | 说明 |
|-----|------|------|------|
| 4 | 0.003 | 0.003(8次) / **12.8(20次)** | 多数完美,偶发整段坏 |
| 6 | 0.003 | 0.005 | 同上,偶发 |
| 8 | 0.003 | 18.3 | 偶发坏更频繁 |

- 单次捕获是**双峰**:要么 0.000%(完美),要么整段 6–18%(坏),没有中间态。
- 坏捕获的 decile 剖面 = **干净段 + 脏段**,边界位置随机(mid-capture sync-loss)。
- 坏捕获换 parity/phase **救不回来**(内部真损坏,非全局配对偏移)。
- 原始 nibble 层面结构均匀(HSYNC 密度处处 ~245/6KB),坏在**去帧**层 → 少量散落 nibble 错打断 TPIU 帧对齐一段。
- /8(10.5MHz)**整体失败**(17–21%,0 锚点)——舒适区上限 12.5MHz 的理论值偏乐观,实际 ~5MHz 以上就开始不稳。

### 9.3 根因判断(标注把握度)

- **已排除**:读出(gen 确认 + drop-first,h2 等多次 0.000%)、采样架构逻辑(SELFTEST 干净时钟 99.998%
  连续)、EYE 相位(各值都偶发坏)。
- **指向(推断,待证)**:真实 STM32 TRACECLK/数据经物理链路(杜邦线/面包板)的**边沿质量**导致 ref 域
  2-FF 边沿检测**偶发误判**(多检/漏检/亚稳),一旦错位就连坏一段直到自然重对齐。SELFTEST 用的是
  FPGA 内部干净方波,所以不出现——这正是 SELFTEST(99.998%)与真实(双峰间歇坏)的差异来源。
- **频率相关性**:1.31MHz 偶发、5.25MHz 更频繁、10.5MHz 全坏 → 与"边沿/采样裕量随频率收紧"一致。

### 9.4 下一步(targeted,不再调参打地鼠)

1. **加深 TRACECLK 同步链 + 边沿检测去毛刺**:把 tck_sync 加到 3–4 级,并要求边沿后电平**连续 N 拍稳定**
   才认边沿(数字去抖),抑制振铃/慢沿造成的多检/误检。这是针对"间歇 sync-loss"的直接 RTL 对策。
2. **用 LA 同步对拍实测边沿质量**:在 /64 同时 LA 抓 TRACECLK + FPGA 抓,定位坏段对应的真实波形,确认是否
   边沿质量问题(拿可对齐 ground truth,别再跨会话猜)。
3. 若坐实是物理边沿质量 → 阻抗匹配/短线转接板(最初就规划的 SI 路线)。


## 10. 采样方案的频率窗口(IDDR+IDELAY vs 过采样)—— 选型定论

### 10.1 orbtrace 的采法(实读 trace/glue.py)

纯源同步 IDDR:把 **TRACECLK 当 FPGA 采样时钟**(进时钟域),每 lane 用 `DDRInput`(ECP5→IDDRX1F)
在 TRACECLK 双沿采,trace_a=上升沿/trace_b=下降沿。**无过采样、无 ref 时钟、无边沿检测、无 CDC。**
采样相位靠 ECP5 IDDRX1F 输入路径**固有时序天然落在眼内**(testbench 注释 "don't worry about phase")
——碰运气式、依赖器件、无显式校准。所以它**没有**我们过采样那个"边沿误判 → mid-capture sync-loss"的
失败模式(它不判断边沿),但移植性差、无相位裕量保证(Xilinx IDDR 无 ECP5 那个天然偏移,直接抄会坏)。

### 10.2 IDELAYE2 硬参数(Artix-7)

- ~78 ps/tap,常用 0–31 tap → 最大延迟 ≈ **2.5 ns**(用满 63 tap 也就 ~5ns)。

### 10.3 IDDR+IDELAY 的最低可用频率

edge-aligned 源要把数据移到半位中心 = 移**半个 UI**:

| TRACECLK | 半 UI | IDELAY(max 2.5ns)够移? |
|----------|------|------|
| 100MHz | 5ns | ✅ 勉强 |
| 50MHz | 10ns | ❌ 差 4× |
| 10MHz | 50ns | ❌ 差 20× |
| 1.3MHz | 380ns | ❌ 差 **150×** |

→ **IDDR+IDELAY 最低可用 ≈ 50–100MHz。低于此,IDELAY 移不动半 UI,采样点永远卡在跳变区。**
(注:高频时 IDELAY 不是用来"移半个低频 UI",而是 UI 本身只有几 ns,2.5ns 范围正好用来扫过整个眼找
中心——配合 ISERDESE2 + 训练序列。这是它天生的高频定位。)

### 10.4 两方案频率窗口几乎不重叠(选型定论)

| 频率 | IDDR+IDELAY | 过采样(ref=200MHz) |
|------|------|------|
| 1–12MHz | ❌ IDELAY 移不动半 UI | ✅ 舒适区 |
| 12–25MHz | ❌ | ⚠️ 可用边缘 |
| 25–50MHz | ⚠️ IDELAY 接近够 | ❌ 过采点不足(双输区) |
| 50–200MHz | ✅ 半 UI≤5ns | ❌ 不可能 |

**结论**:
- 我们的目标频段 **1–12MHz 只能用过采样**;在 /64(1.3MHz)IDDR 物理上不可行(要移 380ns)。
  → 撤销"低速也试 IDDR 对照"的想法(IDDR 在此频段根本不可能工作)。
- 当前 80% 良率的间歇损坏**必须在过采样架构内解决**(边沿去抖/同步加深/SI),不能靠切 IDDR 逃避。
- 上 50MHz+ 是另一套 **ISERDESE2 + IDELAY 扫眼 + 训练**方案(且 UI 小时不需过采样),需更高 SI 质量
  (阻抗匹配板),属后续阶段。
- 提频路径:过采样吃到 ~25MHz(可能要把 ref 提到 400MHz 翻倍)→ 25–50MHz 是难区 → 50MHz+ 转 ISERDES。


## 11. A7 vs ECP5 采集频率能力估算(器件参数 + 实测点)

> 标注:以下为**估算**(基于数据手册标称值 + 我们已有实测点),非逐一实测。约定 4-bit DDR,
> TRACECLK = f,每 lane bit rate = 2f。

### 11.1 内部主频/资源能力

| 资源 | Artix-7 (-1/-2) | ECP5 |
|------|------|------|
| Fabric(一般设计) | ~200–300 MHz | ~100–150 MHz |
| MMCM/PLL 输出 | ~800 MHz(VCO 600–1600) | PLL ~400 MHz |
| IDDR 双沿输入 | C ~600–950MHz → DDR ~1.2–1.9 Gbps/lane | IDDRX1F ~400MHz → ~0.8 Gbps/lane |
| ISERDES 串并 | ISERDESE2 ~1.25 Gbps/lane | GDDRX 同量级 |

A7 高速能力约为 ECP5 的 ~2×。

### 11.2 ETM trace 采集频率窗口估算

**Artix-7**

| 方案 | TRACECLK | 依据 |
|------|------|------|
| 过采样 ref=200MHz | DC–12.5MHz 舒适 / 25MHz 极限 | 半位 ≥8/≥4 ref 周期(本项目实测点) |
| 过采样 ref=400MHz | DC–25MHz / 50MHz | ref 翻倍 |
| IDDR+固有相位 | ~50–200MHz | 低频 IDELAY 补不动半 UI |
| ISERDES+扫眼 | ~100–300MHz+ | 串并+训练 |
| **综合上限** | **~200–300MHz(~0.8–1.2 Gbps/lane)** | IDDR/ISERDES + SI |

**ECP5**

| 方案 | TRACECLK | 依据 |
|------|------|------|
| IDDRX1F+固有相位(orbtrace 实际) | 低频–~200MHz | 固有偏移落进眼;低频眼更宽更易落入 |
| 过采样(若用,ref~300MHz) | DC–~18MHz | fabric/PLL 较低 |
| **综合上限** | **~150–200MHz(~0.6–0.8 Gbps/lane)** | IDDRX1F + SI |

### 11.3 下限与定论

- **过采样下限 = DC**(两器件皆然,有边沿就产 nibble)。
- **IDDR-only 下限**:ECP5 很低(固有偏移落进低频宽眼,orbtrace 能跑慢 trace);**A7 IDDR-only ≈50MHz**
  (无 ECP5 天然偏移,低频 IDELAY 补不动)→ **A7 低频必须过采样**。
- **结论**:A7 上限更高(~200–300MHz vs ECP5 ~150–200MHz);下限两者过采样都到 DC。差别在
  **A7 低频被迫过采样,ECP5 可 IDDR 一招通吃但上限低**。选 A7 用于"低速钉正确性 + 未来冲高速"是合理的,
  代价仅是低频自写过采样(已完成)。


## 12. r16 红队评审后的实验(E1 做完,结果出乎意料)

### 12.1 接受 r16 的核心批评

- **SELFTEST 证过头**:test_clk 与 ref 同源固定相位,每次 re-arm 命中同一好相位桶 → 永远干净;且绕过
  IDELAY(注入 test_data 而非 data_dly)、零 lane skew、50% 干净方波。它只证"FSM 对良性源+好相位无罪",
  不证"对真实信号无罪"。承认。
- **频率相关性证伪纯亚稳态**:1.3M~7-10% / 5.25M 更频繁 / 10.5M 几乎全坏 → 固定时间量 margin 随眼缩侵蚀。
- **双峰需要 per-capture 锁定常量** → re-arm 相位竞态是头号嫌疑(还能解释 SELFTEST 为何永远干净)。

### 12.2 E1 实测:per-capture clean-start(cap_clear)—— 没修好

实现:每次软 re-arm 复位采样器的 seen_rise,强制每个捕获从全新上升沿开始,解耦"捕获起点 vs re-arm 时
锁定的相位"。sim 字节级一致、146 测试过。

板上 30 次 yield:**87% good / 13% bad**,与未改前(90-93%)无显著差异。

**结论**:r16 的头号嫌疑(seen_rise 级的 re-arm 相位竞态)**被证否**。要么 re-arm 不是病根,要么相位
竞态比 seen_rise 更深(采样器整条流水线相对 TRACECLK 的相位,不只是起始门)——但后者用 cap_clear 也
该缓解却没缓解,所以更可能 re-arm 不是主因。

### 12.3 软件版 E0 失败(印证 r16 警告)

err_classify.py 想用 loop 周期性把坏捕获对齐到好捕获做逐 lane/逐 a-b 错误分类。但两个**不同会话**捕获
在本地对齐窗口之后迅速 desync(69% 失配),per-lane 数被错位主导,不可信。**坐实 r16:E0 必须同会话 LA。**

### 12.4 剩下的判别实验都需要硬件介入(交给用户)

无硬件的实验已做尽(读出/CDC/glitch lockout/clean-start/EYE扫/SELFTEST/频率扫)。剩下能定锤的:
- **E0(同会话 LA+FPGA 逐 nibble diff 分类)**:需 LA 接到 trace 线、与 FPGA 同时抓同一段。这是定性
  "错误长相(偏 lane=skew / 偏 a-b=duty / 均匀=亚稳)"的唯一可靠手段。
- **E2(物理回环 SELFTEST)**:FPGA 输出脚发干净 ramp → 短线回环 → trace 输入脚采。区分"FPGA I/O 路径
  (IBUF/IDELAY/反射/duty)" vs "STM32 信号"。需接一根回环线。
- **E3(FPGA 自测真实 TRACECLK 占空比)**:纯 RTL,加 ref 计数器测 FPGA 输入脚处 TRACECLK 高/低拍数
  直方图——这个**我能自主做**,正面验证"FPGA 脚 duty ≠ LA 探头 duty"这个未验证前提。下一步优先做 E3。


## 13. E3 实测:FPGA 输入脚处的 TRACECLK 半周期 dwell

自主做了 r16 的 E3(纯 RTL,无需 LA):在 trace_capture_a7 里用 ref_200m 计数同步后 TRACECLK
(tck_sync[2])的高/低半周期 dwell(单位 5ns),min/max/sum/cnt 经状态寄存器 NB+4.. 读出
(`decode/duty_probe.py`)。

**关键发现(可靠)**:/64(1.31MHz)下,高、低半周期 dwell:
- **max = 77 cyc = 385ns**(= 正确半位 ✓)
- **min = 1 cyc = 5ns** ← **存在 5ns 级的超短半周期(runt/毛刺)**

LA 在 50MSa/s(20ns 分辨率)**根本看不到 5ns 的 runt**——这解释了为什么 §29 LA 测 TRACECLK"边沿
干净、占空比 50.0%"却仍有间歇坏:**真实存在亚 LA 分辨率的短毛刺/亚稳态双采**,落在同步后的时钟上。
注:duty 计数器测的是 raw `tck_sync`(未经 LOCKOUT),所以它看到的是 LOCKOUT 之前的毛刺——LOCKOUT=4
能滤掉 1 cyc 的 runt,但这证明了"毛刺源真实存在",且若有 5–10 cyc 的中等毛刺可能漏过 LOCKOUT。

**不可靠**:avg/sum 数值异常(疑似 idle gap 时 TRACECLK 停拍产生超长 dwell 使 16-bit dwell 计数器
回绕、污染 sum;min/max 不受影响仍可信)。duty% 因此暂不可信,待修(dwell 计数器加饱和、或排除 idle)。

### 13.1 这条线索的意义

- 坐实了"真实信号上有亚 LA 分辨率的时钟毛刺/亚稳态",这是间歇坏的强候选物理来源。
- 但还没证明这些毛刺就是那 7–10% 的直接原因(需要把毛刺事件与坏捕获时间对齐,或加更强去毛刺看 yield)。
- **下一步可自主**:把 dwell 计数器加饱和修好 duty,并加一个"短 dwell(<LOCKOUT 比如 <8 cyc)计数器",
  统计每个捕获里漏过 LOCKOUT 的中等毛刺数,与该捕获 好/坏 关联——若坏捕获的中等毛刺数显著更高,
  就把根因钉死在"毛刺漏过 LOCKOUT"。
- **需要硬件**:E0(同会话 LA 逐 nibble 分类)、E2(物理回环)仍是定性"偏 lane/偏 a-b/均匀"的金标准。


## 14. 真根因找到:捕获起始瞬态(前 ~7.5KB),其余永远干净

### 14.1 关键否证 + 决定性发现

- **E3b 中等毛刺计数**:好/坏捕获的 glitch_cnt 都 ≈0(median 0,max 1)→ "毛刺漏过 LOCKOUT"假说**否证**。
- **逐 nibble 值分布**:坏捕获 gc22 与好捕获 gc25 的 a/b nibble 值分布**几乎完全相同**(逐项差 <0.2%)
  → 坏捕获的原始字节值**没坏**,问题在去帧/对齐,不在采样值。
- **分块解码(决定性)**:把坏捕获切 8 块独立解码——**chunk0(前 7680B)脏 9.8%,chunk1–7 全 0.00%**。
  两个坏捕获(gc20/gc22)**都是同一模式:只有第一块脏,其余完美**。
- **验证修复**:丢掉前 8KB 再解码,两个坏捕获 → **0.000% / 0.009%,33 锚点**。

### 14.2 真根因

间歇性 ~7–10% 坏**不是**:毛刺/SI/采样相位/re-arm 相位竞态/纯亚稳态/读出(全部已逐一否证)。
**而是**:**捕获起始的前 ~7.5KB 是瞬态垃圾**(re-arm 后采集偶尔从 TPIU 帧中间开始 / 帧锁定前就开始写),
之后永远锁定干净。约 70% 的捕获 chunk0 恰好干净起步 → 全程 0%;约 30% chunk0 起步未对齐 → 前块脏、
污染全局 phase 选择 → 看起来"整段坏"(其实只有头坏)。

这也解释了之前所有困惑:双峰(chunk0 对齐与否是二值)、"均匀脏"(脏的头块拖低全局 phase 使整体看着均匀)、
频率相关(高频帧密、起始未对齐的字节数占比变化)、SELFTEST 永远干净(内部源相位固定,每次都对齐起步)。

### 14.3 修正:不是单纯起始瞬态,是**散布的 ~1KB 脏窗口**(可恢复)

skip 8KB 后良率仍 90%。细查(4KB 滑窗精确扫描,不靠粗分块):坏捕获的脏不只在起始——例如 yt18 在
offset 8192 脏、9KB–21KB 干净、**22KB 又脏 10%**、之后恢复。即**每隔一段出现一个 ~1KB 脏窗口,解码在
每个脏窗口后都能自动重新锁定**。之前"只有 chunk0 脏"是 8-分块太粗 + 全局 phase 被任一脏窗口拖低造成的
假象(又一次粗粒度测量误导,记录在案)。

**真实画像**:原始字节值正常(分布与好捕获一致),偶发 ~1KB 局部去帧失锁窗口,散布在捕获各处,每个窗口
后自动恢复。固定 skip 不可靠(窗口位置/长度可变)。

### 14.4 修复方向(更新)

- **解码侧(本质,推荐)**:TPIU 去帧做**局部重锁**——遇到一段解不动就前跳找下一个帧边界重新锁定,
  不让单个脏窗口污染全局 phase 选择。这样无论脏窗口散布在哪都能跳过、保留其余干净数据。
- **采集侧**:start-on-sync 仍有益(消除起始那个窗口),但解决不了中段散布窗口。
- 固定 lead-in skip:**否决**(窗口可变,不可靠)。

下一步:实现解码侧局部重锁 deframe,用 yield_test 验证良率→100%(按"可恢复字节数/锚点全中"判定)。


## 15. 参考调研:类 ETM 信号 + 去帧重锁开源参考(sigrok arm_tpiu)

### 15.1 ETM 并口的信号类比(采集层 / 去帧层)

ETM 并口 = 源同步 + DDR + edge-aligned + 连续流 + TPIU 16 字节定长帧。最贴近的成熟领域:
- **采集层**(DDR 并行流进 FPGA):= DDR SDRAM DQ/DQS 读、并行摄像头/LCD(PCLK+并行)。开源参考:
  **LiteDRAM PHY 的读眼训练/per-bit deskew**(高速档 IDDR+IDELAY 扫眼的权威)、Xilinx XAPP585/524、
  OV5640+以太网传图工程(github,与我们 trace→以太网链路同构)。
- **去帧/重锁层**(连续流找帧边界、失锁重锁):= JESD204 frame alignment、8b/10b comma 对齐、HDMI word
  align。直接同领域开源:**sigrok libsigrokdecode `arm_tpiu`**、OpenCSD deformatter、orbuculum。

### 15.2 sigrok arm_tpiu 重锁机制(精读 decoders/arm_tpiu/pd.py)

三个独立重同步机制:
1. **gap reset**:字节间隔异常长(`ss-prevsample > byte_len`)→ 清空帧缓冲(对应 trace idle gap)。
2. **独立 FSYNC 扫描(核心)**:滚动保留最后 4 字节,一旦 == `FF FF FF 7F`(FSYNC)无条件清空帧缓冲重新
   对齐。注释:"Sync packets override everything else, so that we can regain sync even if some packets
   are corrupted." ← 这就是"丢锁后重锁"的精髓,独立于 16 字节帧计数。
3. 满 16 字节才 process_frame。

### 15.3 适配我们的关键差异:无 FSYNC,但有密集 HSYNC

实测我们的流(yt18 坏捕获):**FSYNC=0,HSYNC(FF 7F)=551 个**,间隔规律(62 的倍数)。
→ sigrok 靠 FSYNC 重锁我们用不了,但 **HSYNC 可作帧相位锚点**(HSYNC 只落在 TPIU 帧内固定偶字节边界,
其位置约束了帧 phase)。

**我们的重锁策略(sigrok 思路的 HSYNC 变体)**:去帧时持续扫 HSYNC,用它校验/强制帧边界对齐;某段
phase 与就近 HSYNC 指示不一致即判为失锁窗口,用 HSYNC 重新对齐,使局部脏窗口不污染其余。这直接对应
§14 发现的"散布的可恢复脏窗口"。

### 15.4 下一步

实现 `etm35lib` 的 HSYNC 锚定局部重锁去帧(替代单一全局 phase 硬切),用 yield_test 验证良率→100%。
高速档采集训练(>25MHz)留待后续,参考 LiteDRAM 读眼训练。


## 16. 真根因最终定形 + 解码侧局部重锁(部分缓解)

### 16.1 根因最终形态(nibble 层证据)

逐 nibble 窗口扫描坏捕获(yt18):绝大多数窗口 parity=0/order=1 解出 0.0%,只有 **3 个 ~1KB 窗口**
(nib 0 / 28000 / 88000)解不动且"最佳"落到相反 parity——即这些窗口里发生了**奇数个 nibble 的
插入/丢失**(漏采/多采一个 TRACECLK 边沿)。但脏窗口**前后 parity 一致**(都 0,1),说明窗口是
**局部损坏后自愈**,不是永久错位。

后果链:nibble 层局部损坏 → assemble 后在 TPIU 字节层造成帧边界偏移 → **单一全局 TPIU phase 解不动
脏窗口之后的整条尾巴** → 整捕获评分 ~10-14%。但其实只有那几个 ~1KB 窗口真坏。

### 16.2 解码侧修复:per-window 局部 phase(`etm35lib.tpiu_deframe_local`)

参照 sigrok arm_tpiu "丢锁重锁"思路(我们无 FSYNC,改用局部 phase 重搜):assemble 取全局最佳 parity 后,
**按 window(默认 5000B)分段,每段独立搜最佳 TPIU phase 再去帧**,把脏窗口的帧偏移**限制在该窗口内**,
两侧干净区照常 0%。已接入 `fpga_la_crosscheck.deframe_raw` / `fpga_errrate` / `yield_test`。

实测(20 次新捕获):
- **全局单一 phase**:双峰 0% / 10-14%,坏率 ~7-10%。
- **局部 phase**:全部压到 **median 1.5% / max ~4.8%,flash 锚点 36-38 全恢复**(LA 金标准才 33,因为我们
  连脏窗口外的更多区段都解出来了)。**最坏情况 14% → ~2-5%,且程序锚点全中。**

### 16.3 诚实的局限(未达 0%)

- 残余 ~1.5% = 真损坏的窗口字节 + **窗口接缝处丢一帧**(固定分窗的代价)。
- 试过"自适应保持 phase 只在劣化时重搜"想消接缝损失,但实现有 bug 反而回到 ~11%,已回退到分窗版。
- **真正干净的解法**:连续帧行走器,中途无缝重新对齐 phase 不丢帧——是对的方向但要仔细写,列为后续。
- 根上的解法仍是采集侧别丢/多 nibble(漏采/多采边沿),但那要么更强去毛刺、要么 start-on-sync、
  要么 SI 改善;解码侧局部重锁是"既然偶发损坏不可避免,就把损坏限制在局部"的稳健兜底。

### 16.4 现状结论

低速下:**程序指令流锚点 100% 恢复**(每次捕获 flash 锚点全中、无杂散),unknown 残余 ~1.5%(局限于
偶发损坏窗口+接缝)。相比起点(整段坏、双峰 7-10% 失败)是实质改善。要做到逐字节 0%,需无缝帧行走器
(解码侧)或采集侧消除 nibble slip(物理/RTL),作为后续。


## 17. 残余 1.5% 的归属:是解码接缝假象,不是 ETM 内容

逐字节查残余 unknown(lf7,0.33%):值主要是 0x2e(55)/0x36(28)/0x3a(22)…,bit0=0,落在 ETM
**Exception Information Byte**(IHI0014Q Fig 7-2:`C|Alt|Can|Exc[3:0]|NS`)的编码空间,初看像异常字节。

**但证据否定"真异常字节":**
1. **前驱不对**:这些 unknown 大多跟在 **P-header** 后(104 个),而 Exception Info Byte 必须跟在 **branch
   地址包**后。对不上。
2. **金标准对照(决定性)**:LA 金标准解同一程序,136K 字节仅 **2 个 unknown(0x1e)**,**无任何 0x2e/
   0x36/0x3a**。若是真 ETM 内容,LA 也该有——没有。

→ **结论:我们的 ~1.5% 残余 = 局部 phase 去帧在窗口接缝/脏窗口边缘的错位字节,不是 ETM 信号**;落进
Exception-Info 编码空间是巧合(bit0=0 的字节大量落在那片)。

**而 LA 那 2 个 0x1e 是真的**:解码为 Exception Information Byte、Exception=15 = Cortex-M **SysTick 异常**
(与 §16 一致)。即 LA 的 ~0.0015% 残余是真实异常信息字节,分类器未建模,非错误。

**含义**:残余不是硬件丢数据、也不是真 ETM 内容,是**解码管道接缝损失**——可由无缝连续帧行走器消除。
干净目标 = LA 的 ~0.0015%(只剩真 SysTick 异常字节)。要达到它,做无缝帧行走器(解码侧),而非动硬件。


## 18. 无缝帧行走器 —— 残余降到 LA 水平(低速 100% 达成)

`etm35lib.tpiu_deframe_walk`:连续逐帧解码,滑窗监控最近 N 帧的 unknown 率,**只在真失锁时**就地前扫
重新对齐(不固定切窗,故干净段零接缝损失)。

实测(18 次新捕获,与固定分窗 `tpiu_deframe_local` 对比):
| 方法 | median unk | max unk | flash 锚点 |
|------|-----------|---------|-----------|
| 固定分窗 (local) | 1.30% | 3.61% | 36-39 |
| **无缝行走 (walk)** | **0.00%** | **0.01%** | **38-39** |

LA 金标准经 walk 解码:**2 个 unknown(0.0015%)= 真实 SysTick 异常字节,131 锚点**,无回归、无误重锁。

→ **低速 100% 正确达成**:每次捕获 unknown ~0.00%、程序锚点全恢复(38-39，比 LA 窗口还多),残余仅
等于 LA 的真实异常字节水平。已设为 `deframe_raw`/`fpga_errrate`/`yield_test` 默认。148 测试通过
(新增 2 个 walker 测试:干净流不劣化、插入垃圾窗后能重锁恢复)。

剩下的真物理残差(偶发 nibble slip 本身)被解码侧无缝吸收;若要从根上消除(为提频做准备),仍是采集侧
(更强去毛刺 / start-on-sync / SI)的后续工作。但就"低速解出完整无错指令流"这个里程碑而言:**达成**。


## 19. 提频摸底实测(walk 解码器,gen 确认,多次重复)

TRACECLK = HCLK/2,HCLK = 168MHz/DIV。每点多次重复 + gen 确认新捕获 + 无缝 walk 解码。

### 19.1 默认 EYE 扫频(找悬崖)

| DIV | TRACECLK | unk 中位 | unk 最差 | 锚点 | 结论 |
|-----|----------|---------|---------|------|------|
| /64 | 1.31MHz | 0.003% | 0.010% | 38 | ✓ 完美 |
| /16 | 5.25MHz | 0.003% | 0.008% | 38 | ✓ 完美 |
| /8 | 10.5MHz | 0.003% | 14.4%(EYE 默认 16 偏) | 38 | ⚠ 需调 EYE |
| /4 | 21MHz | 18% | 18% | 3 | ✗ 崩 |
| /2 | 42MHz | 19% | — | — | ✗ |
| /1 | 84MHz | 20% | — | — | ✗ |

### 19.2 EYE 随频率自适应是关键

10.5MHz(半位 ~47ns ≈ 9.5 ref 周期)EYE 扫描:

| EYE(ref周期/ns) | unk 中位 | 最差 |
|------|---------|------|
| 2 (10ns) | 0.003% | 0.008% |
| 3 (15ns) | 0.000% | 0.003% |
| 6 (30ns) | 0.000% | 0.003% |
| 8 (40ns) | 20% | 崩(超出眼,逼近后沿) |
| 16(默认) | — | 14% 偶崩 |

→ **EYE 必须按频率缩放(≈半位/3~1/2),固定拍数高频会冲出眼**。10.5MHz @EYE=3:**15/15 次,中位
0.003% 最差 0.008%,0 杂散——稳。**

21MHz(半位 ~24ns ≈ 4.8 ref 周期)小 EYE(1/2/3)也全崩(16-24%):过采点不足(±1 边沿抖动 + 2-FF
同步吃掉裕量),到了过采样物理下限。

### 19.3 实测结论:过采样 ceiling ≈ 10.5MHz(悬崖在 10.5–21MHz 之间)

- **稳定上限:TRACECLK 10.5MHz(/8),EYE=3,15/15 完美。**(ref=200MHz 下)
- 悬崖:10.5MHz 稳 → 21MHz 死,中间无 DIV 档位(HPRE 无 /6)。
- 与理论估算(§8:舒适 ≤12.5MHz、可用 ≤25MHz)吻合;实测可用上限落在估算的舒适区顶。
- **要再往上**:(a) ref 过采时钟 200→400MHz(上限翻倍到 ~20MHz);(b) >25MHz 切 IDDR+IDELAY 扫眼
  (源同步,§10);(c) EYE 做成按当前 DIV 自动设(现在手动扫)。

### 19.4 顺带:EYE 应随频率自动设(待办)

当前 EYE 是运行时 CSR 但要手动给值。可加一个"按检测到的 TRACECLK 半位自动设 EYE=半位/3"的逻辑
(用 §13 的 dwell 测量得到半位),省去每频率手扫。


## 20. 性能链路打通:ETM 指令流 → Perfetto(orbetto/Mortrall)

目标:把低速已解出的指令流接到可视化/性能分析后端,链路打通后剩下就是提性能。

### 20.1 选型:Auterion orbetto / Mortrall

`embedded-debug-tools/ext/orbetto`(平级 clone)。关键发现:它的 `mortrall.hpp` 就是**并口 ETM 指令
trace → Perfetto CallStack** 解码器(基于 orbuculum mortem 改),正是我们需要的下游。硬件经验也和我们
一致(去 LED、lane 不等长、"<100MHz 才行")。**与我们 ETM 并口链路天然契合,不用自写 Perfetto 后端。**

### 20.2 接入(三个适配点)

1. **构建**:meson + ninja,子项目(orbuculum/libdwarf/croaring/perfetto)自动拉,已编出 `build/orbetto`。
2. **ETM 协议**:Mortrall `_init()` upstream **硬编码 ETM4**(其 F765/H7 目标),而 STM32F429 是
   **ETMv3.5**。改成 `TRACE_PROT_ETM35`(解码体本就支持 ETM35 disposition 模型)。
3. **ELF device hint**:`Device()` 要求 ELF 名含已知 hint(v5x/nuttx…)否则 assert,ELF 名加 `nuttx`。
4. **TPIU 封装**:orbetto 用 `-t 2` 走自带 TPIU 去帧,路由 stream-id 2 → ETM;但它需要 **FSYNC** 锁帧,
   而我们的流只有 HSYNC 无 FSYNC → 它锁不住。解法:`decode/etm_to_tpiu.py` 把我们**已去帧的干净 ETM
   字节**重新封成带 FSYNC、stream-2 的 TPIU 帧喂进去(绕开 orbetto 锁不住的部分,复用我们已验证的去帧)。

### 20.3 结果(链路通)

金标准 ETM(818 锚点)→ etm_to_tpiu → orbetto -t 2:
- 之前(ETM4 init / 无 FSYNC):**PC bitmap cardinality = 0**(没解出)。
- 修后(ETM35 + 重封装):**PC bitmap cardinality = 29**,`orbetto.perf` = **5.9MB** Perfetto 数据。

→ **完整链路打通**:STM32 ETM → FPGA 4-bit DDR 采集 → 去帧/walk 重组 → TPIU 重封装 → orbetto/Mortrall
(ETM3.5)→ Perfetto。29 个不同 PC 与 proj_add 循环规模吻合。.perf 可拖进 perfetto UI。

### 20.4 验证:PC 与程序逐一对上(非乱码)

orbetto -v3 输出的执行 PC 直方图(命中次数):
```
0800fb6 ×1057823   0800f9c ×889101   0800f8c ×635138(add)  0800fae ×422980
0800fa4 ×127221(loop_sum)  0800fbc ×106057  0800fba ×84908  0800f96 ×84596 ...
```
全部落在 proj_add 循环体 0x08000f8c–0x08000fc0,热指令命中数主导,分布合理;仅 1 个 0x0800120c
(flash 段尾)和 0x08000bbc(一次性 init 分支)。→ **确认是真实指令执行数据,Mortrall 正确重建了
程序执行流。**

### 20.5 待办（更新）
- ✅ 执行 PC 与 loop_sum/add 地址对上。
- 用真实 FPGA 抓的流(非 LA 金标准)跑通同一链路。
- CallStack 线程视图 NuttX 专属(裸机不需要);指令/PC 时间线通用够用。
- 上游差异(ETM35 init、device hint、FSYNC 封装)记为 fork orbetto 的改动点。



## 21. 时间戳核验:无 cycle-count → 时间轴退化(根因 + 修法)

Perfetto 里 slice(loop_sum/add)出来了,但时间戳"看着不对"。写了 `decode/perf_timecheck.py`(无 perfetto
库,纯 protobuf 线格式解析)核验 `orbetto.perf` 的 ftrace 事件时间戳:

- 253790 个事件,时间跨度 **7027 秒**(裸机小循环实际只跑了毫秒级)——离谱。
- delta 双峰:**253780 个事件 delta=1ns**(指令被挤在 1ns 间隔),夹杂几个 **~334 秒的巨跳**。
- slice 样例:`loop_sum @1ns`、`add @334634824821ns`、`E|0 @5354157015619ns`…

### 21.1 根因(已定位)

orbetto/Mortrall 的时间戳 = `cycleCount × 1e9 / cps`(cps 来自 `-C`)。cycleCount 来自 **ETM cycle-count
包**。实测 `-v3` 输出 **"Cc:" 计数 = 0** —— 我们的流里**没有任何 cycle-count 包**。原因:ETMCR=0x980,
**CYCACC(bit12=0x1000)= 0**,cycle-accurate 关着。所以 Mortrall 无真实时间基:大部分指令 ts 不前进
(delta=1ns 退化插值),偶发把未知/野 cycleCount 乘进去 → 334 秒级假跳。

→ **slice 的顺序/内容是对的(指令流正确重建),但时间轴是假的**,因为源端没发周期信息。

### 21.2 修法

1. **开 cycle-accurate**:ETMCR 置 CYCACC(bit12)→ 0x980 | 0x1000 = **0x1980**,让 ETM 发 cycle-count
   包。Mortrall 即有真实周期时间基。需在 `target/etm_enable.cfg` 改 ETMCR 写值并复测。
   - 注意:cycle-accurate 会显著增加 trace 数据量(每段带周期数),低速 60KB 缓冲可能更快填满;且高频
     时加重带宽——提频阶段要权衡。
2. **cps 要对**:`-C` 给的是 **CPU 周期/秒(KHz)**,必须等于产生 cycleCount 的那个时钟。我们 /64 时
   HCLK=2.625MHz → `-C 2625`。但 cycle-count 计的是 **CPU 周期还是 TRACECLK 周期**要核(ETM cycle count
   通常是 CPU 时钟)。开了 CYCACC 后用已知时长程序标定。

### 21.3 现状

- 性能链路(ETM→Perfetto)**结构通**,指令流正确;**只差真实时间基**。
- 下一步:etm_enable.cfg 开 CYCACC → 重抓 → perf_timecheck 复验时间戳是否变合理(毫秒级跨度、
  循环周期间隔均匀且与 HCLK 吻合)。


## 22. cycle-accurate 不被本芯片支持 → ETM 无原生时间基(实证)

按 IHI0014Q §3(6056-6073)的官方测试法验证 cycle-accurate 支持:写 ETMCR bit[12]=1 再读回。
- 写 0x1980,读回 **0x980**(bit12=0)。复测:写 `v|0x1000` 读回仍 0x980。
- Table 3-10:bit[12] 读回 0 = **cycle-accurate tracing 不支持**。

→ **STM32F429 的 Cortex-M4 ETM 不实现 cycle-accurate**(与 M4 ETM 精简一致:无 data trace、无周期计数)。
故 **ETM 指令流在本芯片上不携带任何原生时间信息**;Perfetto 时间戳乱(§21)不是我们的 bug,是源端
没有时钟数据,Mortrall 无 cycle-count 时插值退化。

### 22.1 时间轴的现实选项

1. **指令序时间轴(order-only)**:承认无 wall-clock,只保证指令顺序正确(当前已做到)。修 Mortrall 的
   无-cc 插值,让它给均匀递增 ts(而非 1ns 挤叠 + 334s 假跳),至少 Perfetto 里顺序/嵌套正确可读。
2. **I-sync 周期当粗时间锚**:ETM 每 1024 字节发周期性 I-sync(ETMSYNCFR 锁死 1024)。这是**字节域**
   等间隔,不是时间域;但若 trace 带宽恒定,可粗略映射时间。精度差,仅作粗轴。
3. **ITM 全局时间戳**(若要真时间):ITM/DWT 的 timestamp 包带真实周期数(走 SWO 或 TPIU stream-1)。
   这正是 orbetto 原生支持的(`_handleTS`)。要真 wall-clock,需**同时开 ITM 时间戳 + ETM 指令流**
   (TPIU 多路复用 stream 1+2),用 ITM TS 给 ETM 段打时间锚。这是 orbetto/PX4 的标准做法。
4. **外部时间**:LA/FPGA 采集时打硬件时间戳(我们 FPGA 采集端可以给每个 TRACECLK 周期计数 → 真实采集
   时间),作为权威时间基注入。**这条最适合我们**:FPGA 本就有 ref_200m,可在采集时给字节流打 5ns 分辨
   率时间戳,完全绕开"ETM 无周期"的限制。

### 22.2 结论与下一步

- 性能链路(ETM→指令流→Perfetto slice)**结构与内容正确**;唯一缺的是时间基,且**本芯片 ETM 给不了**。
- 最干净的真时间基 = **方案 4(FPGA 采集端打时间戳)** 或 **方案 3(叠加 ITM 时间戳)**。
- 短期可做方案 1(修 Mortrall 无-cc 插值给单调均匀 ts),让 Perfetto 至少顺序正确好看;真性能分析再上
  方案 3/4。
工具:`decode/perf_timecheck.py`(纯解析 .perf 时间戳)、`target/etm_enable_cycacc.cfg`(验证用,确认
不支持)。


## 23. 重大修正:ETM3.5 **支持** timestamp(我之前查错了特性)

§22 的结论"本芯片 ETM 无时间基"**错了**。我把 **cycle-accurate(ETMCR bit[12])** 和 **timestamp
(ETMCR bit[28])** 搞混了——它们是两个独立特性:

| 特性 | ETMCR 使能位 | 支持指示 | 本芯片 |
|------|-----------|---------|--------|
| cycle-accurate(周期计数) | bit[12] | 写 bit12 读回 | **不支持**(读回 0)|
| **timestamp(时间戳)** | **bit[28]** | **ETMCCER bit[22]/[28]** | **支持** ✓ |

实证(IHI0014Q §7.7 + §3.5.41):
- **ETMCCER = 0x18541800**:**bit[22]=1 且 bit[28]=1** → timestamping 已实现(bit29=0 → 48-bit TS 包)。
- 之前 ETMCCER 读到 0 是**地址读错**(用了 0xE0041040,正确是 word offset 0x7A = 0xE00411E8)。
- 置 ETMCR bit[28]:读回 **0x10000400**(stick,非 RAZ/WI)→ timestamp 可使能。
- 开 timestamp 重抓(`target/etm_enable_ts.cfg`,ETMCR=0x10000980):流里**出现 11 个 timestamp 包**
  (0x42 头,§7.7.4 T-Sync/timestamp),之前是 0。

→ **ETM3.5 时间戳本就支持,只是 etm_enable.cfg 没开 bit[28]。** Perfetto 时间戳乱的根因是**没使能
timestamp**,不是芯片不支持。§22 的"无原生时间基"作废。

### 23.1 待办
- 开 timestamp 后 unknown 升到 ~23%:timestamp 是多字节(头 0x42 + 连续值字节),我们的分类器把连续
  字节当 unknown、walk 解码也要正确**消费** timestamp 包(§7.7.4 格式:1 头 + 最多 9 值字节,C 位续接)。
  需在 etm35lib 加 timestamp 包解析(消费其长度),并把时间值喂给下游。
- 喂 orbetto:orbetto 有 `_handleTSFromETM(cc)` 走 ETM 时间戳路径;确认 Mortrall 是否解 ETM3.5 TS 包
  (它原本为 cycle-count 设计)。可能需让 orbetto/我们解析 TS → 设 cps = timestamp generator 时钟。
- 标定时间值单位:48-bit timestamp 来自 timestamp generator(常为系统计数器),`-C` 要对应它的频率。


## 24. timestamp 包出来了但值恒为 0 → F429 无系统时间戳源(最终定论)

开 timestamp(ETMCR bit28)后,正确对齐(swap=0,parity=0,order=1)解码:**0.00% unknown,39 个
timestamp 包**——干净。但**所有 timestamp 值都是 0**。

之前测到 23% unknown 是我 inline 脚本选错了 parity/order(latch 到 0x8080808 噪声"锚点");正确对齐
下流是干净的,loop PC(0x08000fxx)全对。

### 24.1 值恒为 0 的含义(查 ETM 文档 + M4 TRM + ROM 表实证)

先厘清"时间源到底从哪来"这个问题(用户问:ARM 内核指令计数器?ETM 自带计数器?):

- **不是 ARM 内核指令计数器**:M4 没有把执行的指令数喂给 ETM 当时间。
- **不是 ETM 自带计数器**:ETM3.5 的 cycle-count(Cc)字段需要 cycle-accurate 模式,而 F429 的 ETM
  **不实现** cycle-accurate(§22 实证:ETMCR bit12 写不进)。所以 ETM 自己也产不出时间。
- **是 SoC 的独立时间戳生成器(TSGEN / CoreSight timestamp generator)**:一个自由运行的计数器,经
  APB(CNTCONTROL)广播给所有 trace 源(ETM/ITM)。ETM 只是把这个**外部输入**打进 timestamp 包。

文档依据:
- IHI0014Q §7.7.4:"**A value of zero indicates that the timestamp is unknown.** This might also indicate
  that the implementation does not fully support timestamping."
- DDI0440C(M4 ETM TRM)§2.1.2:"**A system implementation may provide** a timestamp count which can be
  used by several trace sources." → ETMCCER bit22=1 只表示 ETM 端**有能力接**,时间值本身是 SoC 输入。

**ROM 表实证(决定性证据)**:读 CoreSight ROM 表 `0xE00FF000`,F429 只挂了 **6 个组件**——
SCS(0xE000E000)、DWT(0xE0001000)、FPB(0xE0002000)、ITM(0xE0000000)、TPIU(0xE0040000)、
ETM(0xE0041000)。**没有 TSGEN / CNTCONTROL 组件**。ARM 社区也实证过同级小 STM32(如 L433)不集成
TSGEN。

→ **结论:F429 根本没集成 TSGEN 硬件块。** 不是"timestamp 没使能"(没有寄存器可开),而是芯片物理上
没有这个时间源。ETM timestamp 机制实现了(包能发)、bit28 能置位,但喂进来的时间输入恒为 0。
M4 ETM 无 cycle-accurate(§22)+ SoC 无 TSGEN(本节,ROM 表实证)= **F429 上 ETM 拿不到 wall-clock,
且无任何寄存器开关能改变这一点。**

### 24.2 最终时间基方案:FPGA 采集端打时间戳(方案 4)

既然源端给不了时间,**由我们 FPGA 采集端提供权威时间基**:用 ref_200m(5ns)在采集每个 trace 字节
(或每个 TRACECLK 沿)时打一个计数器时间戳,与字节流一起存。这:
- 完全绕开"F429 ETM 无时间源"的限制;
- 是真实的采集 wall-clock(5ns 分辨率,远超需求);
- 天然适配我们的架构(FPGA 已有 ref_200m + BRAM)。
下游把"第 N 个 trace 字节 → 采集时间"映射进 Perfetto(替代 Mortrall 的 cycleCount 路径)。

### 24.3 现状
- 性能链路 ETM→指令流→Perfetto:**内容/顺序 100% 正确**;
- 时间基:ETM 原生不可得(已彻底查清),**改由 FPGA 采集端提供**(待实现)。
- timestamp 包解析已验证(能从流里提取,只是值为 0);etm_enable.cfg 是否常开 bit28 可选(值为 0 时
  无意义,反而增加流量,**建议默认不开**,除非将来芯片有 TS 源)。


## 25. FPGA 采集端时间戳(方案 4 实现 + 实测)

§24.2 的方案落地:既然 F429 ETM 给不了 wall-clock,由 FPGA 采集端提供权威时间基。**实测打通,真实硬件
0.000% unknown、时间轴零非单调。**

### 25.1 RTL(trace_stream_top.v,CAP_RAW 路径)

- **自由运行计数器** `cap_clk_cnt`:clk200(ref_200m,5ns/tick)域,每个 ref 周期 +1,软 re-arm 清零。
  这是真实采集 wall-clock,**与目标 TRACECLK 频率无关**——我们给字节打时间,不假设字节速率。
- **稀疏快照表** `tsmem`:每采集 `TS_STRIDE`(256)个 RAW 字节,把 `cap_clk_cnt` 快照进一格
  (distributed RAM,DEPTH=60KB → 241 格 × 4B ≈ 964B)。为什么稀疏而非首尾两点?**TRACECLK 会中途空闲**
  (目标不发数据时的长 0 区);首尾线性模型会把暂停"抹平"到整段。每 256B 一个真实时间快照,使暂停在它
  实际发生的位置体现为时间间隙(stride 粒度)。
- **读出**:UDP :5001 在数据区/状态区之后开了两块——
  - `NB+26..32`:元数据(stride_log2、表格条目数 n、最后一字节的 tick)。
  - `NB+64 .. NB+64+4n`:快照表(每格小端 u32)。
  读出与数据区同样有 1 拍 BRAM 延迟;**字节车道选择器 `ts_lane_d` 必须和 `tsrd` 一起延 1 拍**,否则
  车道与锁存的字相互错位(第一版就是这个 bug:tick 表出现周期-3/4 的乱序;延拍后 0 乱序)。
- 时序:WNS=0.206ns,THS=0,0 errors。bit 存为 `build/trace_stream_ts.bit`。

### 25.2 PC 侧

- `scripts/trace_dump.py --timebase`:抓数据后再读元数据+快照表,写边车文件 `<out>.ts.json`
  (`{stride, n, tick_ns, last_tick, ticks[]}`)。
- `decode/fpga_timebase.py` `TimeBase`:把边车表插值成"任意 RAW 字节索引 → wall-clock ns"。
  处理 32-bit 计数器回绕(>21.5s 才回绕,正常不触发)、尾部插值到最后一字节、skip 偏移。8 个单测。
- `decode/etm35lib.py` `tpiu_deframe_walk_offsets`:在无缝行走去帧的同时,记录每个输出 ETM 字节来自哪个
  RAW 源字节偏移(数据槽与源位置 1:1)。2 个单测。
- `decode/etm_with_time.py`:串起来——RAW 抓取 + 边车 → 去帧(带偏移)→ 每个 ETM 字节的 wall-clock 数组
  `<out>.time.json`。这是替代 Mortrall cycleCount 路径、给 Perfetto 真实时间轴的桥。

### 25.3 实测(/64,TRACECLK≈1.31MHz)

```
trace_dump --timebase : 241 snapshots @ every 256 B, span 49928 us
tick 表               : 0 decreasing, 每 256B 均匀 ~39006 ticks
                        = 195us/256B = 762ns/byte —— 与 /64 DDR 单字节/TRACECLK 周期吻合
etm_with_time         : 61440 RAW -> 39943 ETM bytes, unknown 0.000%
                        time 4.6..46799.8 us, 非单调违规 0
```

频率自适应验证:同一码流换 DIV 档位时,762ns/byte 这个数会随 TRACECLK 自动变化(慢则每字节 tick 多、
快则少),无需改任何参数——因为我们测的是真实采集时间,这正是方案 4 相对"假设字节速率"的优势。

### 25.4 待接(下一步)

把 `<out>.time.json` 喂进 orbetto/Mortrall,用 FPGA 时间替换 `cpu.cycleCount` 路径
(`_handleTSFromETM`/`_flush_proto_buffer` 的 ns 计算),让 Perfetto slice 落在真实 wall-clock 上。
RTL+PC 解码侧时间基已就绪且实测干净;剩下是 Mortrall 注入点改造。
