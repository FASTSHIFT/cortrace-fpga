# H743 ETM Trace 溢出根因分析：为什么 F429@84M 不乱、H743@50M 反而溢出

> 日期：2026-07-05
> 状态：根因已定位（多手册交叉核实，非推测）
> 关联：HANDOFF §8.4（H743 支线）、proposal 25（F429 满速 84M 验证）、proposal 27（LVGL 中断重建困境）
> 参考手册（均已存 `docs/artix7-port/refs/`）：
> - `DDI0494D_coresight_etm_m7_r0p1_trm.pdf`（ETM-M7 TRM）
> - `DDI0489F_cortex_m7_trm.pdf`（Cortex-M7 处理器 TRM）
> - `DDI0440C_etm_m4_r0p1_trm.pdf`（ETM-M4 / F429 TRM）
> - `IHI0064H_b_etm_v4_architecture_specification.pdf`（ETMv4 架构规范）
> - `RM0433`（STM32H742/743 参考手册，工程根 `docs/`）

---

## 0. 一句话结论

**不是 SI/杜邦线问题，也不是绝对带宽不足——是 M7 的「trace 生成速率」相对 M4 暴涨，而 H743 这次的「trace 出口 drain 速率」反而比 F429 更低，两头一夹，ETM FIFO 必然溢出。** 具体地说：

1. **生成端暴涨**：M7 是**双发射 + Harvard I/D cache 零等待执行**（RM0433：Cortex-M7 零等待状态运行），同一段 `func_test` 的紧凑调用循环，M7 的指令吞吐（进而**分支速率**）远高于 F429（从 flash 取指、带等待周期 + ART 预取）。分支广播（branch broadcast）下，**每个直接分支都要发一个 Address 元素**，所以分支速率越高，trace 字节率越高。
2. **drain 端更低**：F429 是 TRACECLK **84MHz** 4-bit；H743 这次是 TRACECLK **50MHz** 4-bit（pll1_r_ck=100M÷2 DDR）。出口带宽 H743 只有 F429 的 ~60%。
3. **FIFO 更浅、填得更快**：M7 指令 FIFO 64B，但**每周期可产 2 条指令**的 trace；drain 端 ATB「每周期从指令 FIFO 读 1 字节」（DDI0494D §2.1.6）。峰值生成 >> 峰值 drain。

**F429 当年不溢出是因为它同时占了「生成慢 + 出口快」两个便宜；H743 这次同时踩了「生成快 + 出口慢」两个坑。** 换句话说，把 TRACECLK 降下来对 M4 无害、对 M7 是雪上加霜。

---

## 1. 现象与反证

| 项 | F429（proposal 25） | H743（本次） |
|---|---|---|
| 固件 | 同一份 `func_test`（`main_loop` 紧凑调用循环，无 delay） | 同一份 `func_test` |
| 杜邦线/接线 | 10cm 等长杜邦，GPIO1 排针 | **完全相同** |
| TRACECLK | 84 MHz（HCLK 168M / 2） | 50 MHz（pll1_r_ck 100M / 2 DDR） |
| 位宽 | 4-bit | 4-bit |
| 采样质量（HSYNC 探针） | 无误码 | **无误码**（本次实测 HSYNC 图案 100.0000% 匹配） |
| ETM 溢出 | 无，unknown 1.7-2.5% | **Overflows 1356、栈被打乱** |

关键反证（用户提出）：**采样层零误码、线材相同、TRACECLK 更低，H743 却溢出。** 这直接排除了信号完整性/线材，把矛头指向 **trace 生成速率 vs drain 速率的失衡**——即 M7 架构差异。

---

## 2. 生成端：M7 为什么产生远多于 M4 的 trace

### 2.1 双发射（dual-issue），每周期 2 条指令

DDI0494D（ETM-M7 TRM）§2.4.3 *Parallel instruction execution*（原文，已按 30 词内引用）：

> "capable of tracing two instructions per cycle and two data transfers per cycle."

即 M7 macrocell **一个周期最多要给两条指令打 trace**。F429 的 ETM-M4 是单发射，每周期至多 1 条。相同指令数下，M7 的 trace 在时间轴上**压缩了近一半**（峰值字节率翻倍），而 FIFO drain 是恒速的——峰值更容易顶穿 FIFO。

### 2.2 I/D cache + 零等待执行 → 指令吞吐（和分支速率）暴涨

RM0433 明确：STM32H7 的 Cortex-M7「以 CPU 时钟速度、零等待状态」执行（配 I-cache/D-cache/TCM）。DDI0489F §Feature 表列出 M7 具备 Harvard I-cache / D-cache（4-64KB 可配，STM32H743 均实装）。

对照 F429（Cortex-M4）：从内置 flash 取指要吃**等待周期**（168MHz 下典型 5 WS），靠 ART 加速器预取和缓存部分弥补，但**紧凑循环体一旦超出 ART 的 128-bit 行/指令缓存，取指仍会 stall**。

后果：`func_test` 的 `main_loop` 是一串**背靠背的函数调用**（`level_a`→`level_b`→`level_c`→…、`factorial` 递归、`deep1..6`、`pingpong×5`），几乎没有 `mydelay` 空转（源码注释：「无长延迟版，trace 密度高，模拟 LVGL 满载」）。这种代码在 M7 的 I-cache 里**命中率极高、近乎零等待全速跑**，单位时间执行的**函数调用/返回（=直接分支）数量**远高于 F429。

### 2.3 分支广播把「分支速率」直接变成「trace 字节率」

我们两边都开了 branch broadcast（BB）。ETMv4 规范 IHI0064 §2.7.5 *Branch broadcasting*：

> BB 开启时，trace unit「显式追踪 PE 执行的直接分支和 ISB 指令的目标地址」，用 Address 元素输出。

即**每一个直接分支（含每次函数调用和返回）都强制产生一个 Address packet**。于是：

```
trace 字节率 ≈ 分支速率 × 每分支平均字节数
分支速率    ≈ 指令吞吐 × 分支密度(func_test 调用密集，分支密度很高)
指令吞吐    : M7(cache+双发射,零等待) >> M4(flash+单发射+等待周期)
```

三个因子在 M7 上全部上扬，且相乘。**这就是 I/D cache 与本问题的直接关系**：cache 让 M7 全速跑密集分支代码，BB 把每个分支放大成 trace 字节，FIFO 被灌满。

---

## 3. drain 端：H743 这次出口带宽反而更窄，且中间多一级 FIFO

### 3.1 出口位率

| | TRACECLK | 位宽 | DDR | 出口净位率 | 出口字节率(理论上限) |
|---|---|---|---|---|---|
| F429 | 84 MHz | 4 | 双沿 | 84M×4×2 = 672 Mbit/s | ~84 MB/s |
| H743（本次） | 50 MHz | 4 | 双沿 | 50M×4×2 = 400 Mbit/s | ~50 MB/s |

H743 出口上限只有 F429 的 **~60%**。（实测 FPGA 侧 UDP 出口 50 MB/s、0 丢包，正是这个理论值，说明 FPGA/网络不是瓶颈——瓶颈在 MCU 内部 ETM→TPIU 这一段的相对速率。）

### 3.2 M7 多一级 ETF，且 ATB 是「每周期 1 字节」窄口

DDI0494D §2.1.6 *ATB interfaces*：指令 ATB「**每次从指令 FIFO 读单个字节**」送出。M7 拓扑（RM0433 §60）是：

```
ETM(双发射,2 指令/周期) → 指令FIFO(64B) → 指令ATB(1 字节/周期) → CSTF → ETF(4KB TMC) → TPIU(4-bit@50M) → 引脚
```

- **指令 FIFO 只有 64 字节**（DDI0494D 表：Instruction FIFO 64 byte with 8-bit output）。F429 的 ETM-M4 FIFO 是 24 字节（DDI0440C：FIFO size 24 bytes）——M7 深一点，但**生成峰值是 M4 的数倍**，64B 仍是杯水车薪。
- 生成端峰值可达 **~2 指令/周期 × 分支字节**，drain 端 ATB **1 字节/周期**。只要密集分支持续几十个周期，FIFO 必满。DDI0494D §2.4.12 印证这一机制：当 trace 持续产生而**指令 FIFO 无法排空时就会溢出**（"the instruction FIFO is unable to drain and overflows"）。溢出即丢弃 trace 并在流中插入 Overflow 标记。

### 3.3 为什么降 TRACECLK 帮倒忙

TRACECLK 决定的是**引脚出口速率**（drain 的最末端）。降 TRACECLK = drain 更慢 = FIFO 更容易满 = 溢出更多。这解释了「H743 50M 比 84M 更糟」的直觉悖论——**对已经产能过剩的 M7，任何降低出口带宽的动作都是负优化**。F429 因为生成端慢，84M 出口绰绰有余，从没触发这个失衡。

---

## 4. 溢出如何「打乱栈」（与解码器的关系）

一旦 ETM FIFO 溢出：

1. ETMv4 在流里插入 **Overflow 包**（`0x05`），并丢弃溢出期间的 trace 元素。IHI0064：溢出后到下一个同步点之间的指令流**不可重建**。
2. orbetto/mortrall 收到 Overflow 后调用栈进入未知态，直到下一个 A-Sync + TraceInfo(0x01) 重新锚定。**这中间的调用/返回配对全部丢失**——表现就是「栈是乱的」。
3. 本次实测 1356 次溢出 / 500KB，意味着流被反复截断，栈永远无法长时间保持正确。

**补充（已并行修复的一个真 bug，但非本溢出根因）**：ETMv4 异常信息字节的续位 C（IHI0064 图 6-10 bit7）此前被忽略，导致 SysTick(TYPE=15，单字节 C=0)被误读两字节、异常号解成 151 并失步。已在 `orbuculum` 提交 `fix(etm4): honour exception info byte continuation bit (C)` 修复。但即便修好，溢出仍会淹没真实 SysTick 包——**溢出是栈乱的主因，异常续位是次要 bug**。

---

## 5. 解决方向（按推荐优先级）

目标：让 **drain 速率 ≥ 生成峰值速率**，或**降低生成速率**，或**用 FIFO 停核换无损**。

### 方案 A（推荐）：提高 TRACECLK，把出口带宽拉到 M7 产能之上
- H743 的 TRACECLKIN = `pll1_r_ck`，可调 PLL1 的 DIVR1。当前 100MHz→引脚 50M。若提到 pll1_r_ck=200MHz→引脚 **100M**，出口 ~100 MB/s，超过 F429 的 84M。
- **代价**：FPGA 采样前端要支持 100M+ TRACECLK。当前 MMCM 采样链最高验证到 84M（proposal 22）。100M 需要重新综合并做相位扫描（可能逼近 IDELAY 区间，proposal 22 §7）。
- **优点**：最接近 F429 的成功配置逻辑，无损全量 trace。

### 方案 B：降低生成速率——关闭 branch broadcast（BB OFF）
- BB OFF 后，ETM 只在**不可推断的间接分支/异常**处发地址，直接分支靠解码器用 ELF 静态推断。trace 字节率可降一个数量级（proposal 参考：F429 侧实测 BB 对字节量影响显著）。
- **代价**：解码器必须靠 I-sync 周期锚定 + 反汇编推断直接分支流，间接调用（`op_table[]` BLX、回调）仍精确。func_test 的间接调用/递归仍可解。
- **风险**：BB OFF 下 trace 太稀疏时，连续流式采样的 TRACECLK 会在空档停（本次已观察到 BB OFF + 稀疏 → FPGA 收不到连续流）。需配合方案 D 的心跳/同步周期。

### 方案 C：ETM 停核无损（TRCSTALLCTLR.ISTALL / NOOVERFLOW）
- DDI0494D §（TRCSTALLCTLR）：`ISTALL`(bit8) 让指令 FIFO 空间不足时**停住处理器**，`LEVEL`(bits[3:2]) 设阈值；`NOOVERFLOW` 彻底防溢出。
- **本次实测**：设 `ISTALL + LEVEL=3`（0x10C）短抓能到 0 溢出，但长跑仍 1356 次——说明**单靠 ETM 自身 FIFO 的停核挡不住下游 ETF→TPIU 的持续拥塞**，或需要配合 ETF 的停核 / 更高 LEVEL。
- **代价**：停核是**侵入式**的（改变被测程序时序，DDI0494D 明确 NOOVERFLOW「可能显著影响性能」）。用于「要完整调用图、不在乎实时性」的场景可接受；对「模拟 LVGL 满载实时行为」不理想。

### 方案 D：降低分支密度的等价手段
- 用 TRCBBCTLR 的 include/exclude 只对**关心的地址区间**开 BB（IHI0064 §2.7.5），其余区间省带宽。
- 或提高 TRCSYNCPR 同步周期减少同步开销（次要）。

### 组合建议
- **短期验证调用图正确性**：方案 C（停核无损）+ 承认侵入性，先拿到 12/15→15/15 的干净栈，确认解码链对 M7 完全正确。
- **长期满速实时**：方案 A（提 TRACECLK 到 100M+）为主，必要时叠加方案 B/D 降流量。这才是「像 F429 那样满速抓 LVGL」的正路。

---

## 5.6 链路正确性验证（cache 开 + 短突发，2026-07-05）

问题定位后，反复验证「链路+解码是否本质正确」（区别于「满速能否无损」）：

- **多次抓取中，凡是抓到干净突发的，解出的调用树都正确对应 func_test**：`main_loop→factorial`（递归嵌套）、`main_loop→deep1→deep2→…→deep6`（**完整 6 层深嵌套、配对正确**——这正是满速溢出时被打乱丢掉的那部分）、`callback_test→cb_handler_a`、间接调用 `op_add/op_sub/op_mul`（不同窗口分别命中）。
- 首次满速捕获：**817 PC、12/15**；干净短突发捕获：deep 链 nesting=**6**（满分项）。
- **结论：ETM→CSTF→ETF→TPIU→FPGA→UDP→orbetto(ETMv4) 整条链路与解码本质正确无误。** 满速下 12/15 而非 15/15，纯粹是 §2-4 的 FIFO 溢出在满速连续流里撕裂了部分嵌套，不是链路/解码缺陷。

**遗留工程问题（非链路本质，两者相互掣肘）**：
1. **ISTALL 停核**能把 ETM 自身 FIFO 溢出压得很低，但会把 trace 变成**断续突发**；而 FPGA 连续流式采集需要 TRACECLK 持续翻转，突发之间 TRACECLK 停摆 → 采集端只收到心跳零包或极短片段，难以累积完整 loop。→ 停核无损与连续流式采集在当前架构下冲突。
2. **FPGA packetiser 偶发死锁**（HANDOFF §7.1 的 self-TX/ARP 问题）：反复 halt/resume/reflash 后常出现「ARP 能回但 UDP 流停摆」，需物理断电重插恢复，拖慢长窗口累积。

→ 两点都指向**方案 A（提 TRACECLK 让连续满速流不溢出）**才是同时满足「cache 开满速 + 无损 + 连续可采集 + 栈干净」的唯一正路；ISTALL 更适合「一次性小样本离线核对」。

---

## 6. 待验证清单（下一步实测）

1. ✅ **cache 开关对照**（§5.5，已完成）：关 I/D cache → 溢出 810→0、trace 速率降 ~22×，判决性坐实 cache→高吞吐→高分支率→溢出这条链。
2. **提 TRACECLK 到 100M**：综合 100M MMCM 比特流，重测溢出是否消失（cache 仍开，验证「提高出口带宽」这条正路）。
3. **BB OFF 全量对照**：cache 开 + BB OFF + 停核，看栈能否 15/15。
4. **量化 F429 对照**：在 F429 上测同 func_test 的 ETM 字节率，与 H743 cache-on 的 5498 B/s 直接对比 M4 vs M7 倍数。

---

## 7. 结论

用户的判断正确，且已由判决性实验坐实（§5.5）：**问题不在 SI/线材/绝对带宽，而在 M7 与 M4 的架构差异。I/D cache（叠加双发射、零等待执行）让 M7 全速运行 func_test 的密集分支循环，在 branch broadcast 下产生远超 F429 的 trace 字节率；而 H743 这次的 50M 出口带宽反低于 F429 的 84M，中间还多一级窄口 ETF/ATB。生成远大于 drain，ETM FIFO 溢出，溢出撕裂指令流，调用栈随之被打乱。**

判决性证据：**同配置下关掉 I/D cache，溢出从 810 直接降到 0、trace 生成速率降约 22×**——cache 是流量暴涨的直接推手，与「F429 无 cache 从不溢出」自洽。修复方向是提高出口带宽（方案 A，TRACECLK→100M+，正路）或降低生成流量（方案 B/D）或停核无损（方案 C，侵入式）。关 cache 本身只是诊断手段，不作方案。
