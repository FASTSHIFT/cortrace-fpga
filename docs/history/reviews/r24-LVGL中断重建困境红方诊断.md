# r24 · 提案 27「LVGL 中断重建困境」红方诊断评审

> 评审对象：`proposals/27-LVGL中断重建困境分析.md`
> 立场：红方，ARM CoreSight ETM3.5 解码 + FPGA 物理层联合诊断
> 日期：2026-07-04
> 目的：帮蓝方定位根因，不是打击方案

---

## 总判断（先给）

**蓝方的概率分析（"2.91% random 吞不掉 95.7% 的异常包"）是正确的，这确实不是随机丢字节能解释的。根因是系统性的。** 但蓝方列了 5 个可能性却没有排序——我帮你排：

**最可能的根因是第 4 条（解码器字节对齐错位），且机制是"级联失锁"而非"单点丢失"。**

具体论证如下。

---

## 1. 核心判断：这是解码状态机层面的系统性失锁，不是物理层丢包

### 证据链

| 观测 | 含义 |
|------|------|
| 2.91% unknown | ~每 34 字节有 1 字节错（物理层本底） |
| 95.7% 异常包丢失 | 远超 random p=0.0291 的预期（应只丢 ~5-6%） |
| orbetto 仍解出 5672 PC + 56K 事件 | **大部分** P-header 和普通 Branch 包被正确解码 |
| 安全网每 ~1ms 触发 | 精确对应 SysTick 周期（不是随机分布） |
| func_test（21M, 0.002% unknown）完美 | 同一解码路径，差异只在 unknown 率 |

**关键悖论**：如果解码器整体"错位"了，应该所有包都解不出——但 56K 事件说明大部分包OK。如果是随机丢字节，不应该只丢异常包。

**唯一解释这个组合的假说**：解码器在大部分时间工作正常（解出 P-header、普通 Branch），但**在中断发生的特定时间点系统性地失效**。

---

## 2. 根因假说：ETM3.5 异常包的"X bit"解析在 nibble-slip 后产生级联效应

### ETM3.5 异常进入包的结构（IHI0014Q §7.2 + §7.6）

ETM3.5 的异常进入**不是独立的 1 字节包 `0x7E`**（那是"data tracing only"的 Exception Entry 包）。在 Cortex-M4 的指令 trace 模式下，**异常进入信息附着在 Branch Address 包的末尾**：

```
[Branch Address 包 (1-5 字节)] + [Exception Info Byte 0 (1 字节)] + [Exception Info Byte 1 (可选)]
```

即：当 CPU 发生异常进入时，ETM 产出一个**普通 Branch Address 包**（目标地址 = 异常向量入口），但在包末尾**追加 1-2 个 Exception Information Bytes**。解码器通过 Branch Address 包的最后一个字节中的 **X bit**（bit 6 in standard format, bit 6 in alt format）来判断"后面还有异常信息字节"。

### 从源码确认（`traceDecoder_etm35.c` line ~326-340）

```c
// terminateAddrByte:
if ( ( !C ) || ( j->byteCount == 5 ) )
{
    cpu->addr = j->addrConstruct;
    
    if ( ( !C ) & ( !X ) )
    {
        /* This packet is complete, so can return it */
        newState = TRACE_IDLE;  // ← 普通 branch，回到 IDLE
    }
    else
    {
        /* This packet also contains exception information, so collect it */
        j->byteCount = 0;
        cpu->resume = 0;
        _stateChange( cpu, EV_CH_EX_ENTRY );  // ← 报告异常进入！
        newState = TRACE_COLLECT_EXCEPTION;    // ← 进入异常信息收集状态
    }
}
```

**X 的计算**（standard format, line ~279）：
```c
case TRACE_COLLECT_BA_STD_FORMAT:
    C = ( j->byteCount < 5 ) ? c & 0x80 : c & 0x40;
    X = ( j->byteCount == 5 ) && C;
    goto terminateAddrByte;
```

**X 的计算**（alt format, line ~268）：
```c
case TRACE_COLLECT_BA_ALT_FORMAT:
    C = c & 0x80;
    X = ( ( !C ) && ( c & 0x40 ) );
    goto terminateAddrByte;
```

### 级联失锁机制

**Standard format 下**（F429 应该用 standard format，因为没有 alt address encoding）：

- Branch Address 包的每个字节的 **bit 7** 是 continuation bit（C=1 表示还有后续字节）
- 最后一个字节 C=0（表示地址结束）
- **X bit = bit 6 of 最后字节（仅在第 5 字节时有效）**

不对——再细看代码：standard format 下 `X = (j->byteCount == 5) && C`。这意味着 **X 只在包到了第 5 字节且 C=1 时才为真**（这是 legacy ARM mode 的异常编码方式）。

**Alt format 下**：`X = ((!C) && (c & 0x40))`——最后字节的 C=0 且 bit6=1 时表示有异常信息。

**关键问题：F429 Cortex-M4 ETM 用的是 standard 还是 alt format？**

查 `traceDecoder_etm35.c` 初始化：`j->usingAltAddrEncode` 默认为 0（standard）。但 DDI0440C (Cortex-M4 ETM TRM) §2.3.1 说 M4 ETM "uses alternative branch address encoding"——**即 F429 应该用 alt format！**

如果 orbuculum 的 `usingAltAddrEncode` 没有被正确设置为 true（基于 ETMCR 的 bit[21] "branch broadcasting" 字段），那整个 branch address 解析就用错了格式——**地址能解出（因为低位编码兼容），但 X bit 的位置解错了 → 异常信息字节永远不会被识别**。

### 验证这个假说

在 alt format 下，异常信息的触发条件是：
```c
X = ( ( !C ) && ( c & 0x40 ) );  // 最后字节的 C=0 且 bit6=1
```

在 standard format 下：
```c
X = ( j->byteCount == 5 ) && C;  // 只有第5字节且C=1才触发
```

**如果 Cortex-M4 的 ETM 发出 alt-format 的异常包，但解码器用 standard format 去解**：
- Alt-format 异常包：最后字节 bit7=0 (C=0), bit6=1 (X=1) → 后面跟异常字节
- Standard-format 解读：看到 C=0 → 包结束，**X = (byteCount==5)&&C = (byteCount==5)&&0 = false** → 不收集异常信息 → **直接回到 TRACE_IDLE**
- 后面的 Exception Info Byte 到达 TRACE_IDLE 状态 → 被当成**新包的第一个字节**解析

**后果**：
1. 异常进入的 `EV_CH_EX_ENTRY` **永远不会被触发**（因为 X 永远为 false）
2. Exception Info Byte（格式 `C|Alt|Can|Exc[3:0]|NS`，bit 0 = NS）被当成 TRACE_IDLE 中的一个新字节解析
3. 如果 Exception Info Byte 的值恰好匹配某个包头模式（比如 bit0=1 → 被当成 Branch Address 包的第一个字节）→ 解码器进入错误的地址收集 → 吃掉后续几个字节 → 地址计算出乱值
4. 最终回到 IDLE 时，**消费了额外 1-2 个字节**（Exception Info Bytes）→ 后续所有包的边界都偏了 1-2 字节
5. 直到下一个 I-sync（0x08）恰好落在正确位置 → 才重新同步

**这就是"放大效应"的精确机制**：不是"一个 nibble-slip 导致后续错位"——而是**每次中断都产生一次 1-2 字节的解析错位**，然后靠 I-sync 重新恢复。在两个 I-sync 之间（1024 字节，在 84 MB/s 下 = ~12μs），**所有中断的异常信息都被吞掉**。

但是等等——如果**每次**中断都导致 1-2 字节错位，那为什么 56K 事件（普通 branch + P-header）还能正确解出？

答案：**I-sync 每 1024 字节出现一次 = 每 ~12μs 恢复一次**。在两个 I-sync 之间的 12μs 里，第一个中断导致错位 → 后续一小段数据（几十到几百字节）乱解 → **然后可能偶然重新对齐**（因为 P-header 是 1 字节包，如果错位后下一个字节恰好是合法的 P-header 格式 → 解码器"自然恢复"）。但异常进入事件**永远丢失**（因为 X 从不为真）。

---

## 3. 进一步确认：为什么 func_test 21M 能工作？

func_test 在 21M（unknown=0.002%）下异常检测正常。如果根因是"格式错配"，它也应该失败。

**可能的解释**：

1. **func_test 禁 SysTick 时**（`functest_nosystick.perf`）完美——这不矛盾（没有中断就没有异常包，不触发问题）。
2. **func_test 有 SysTick 但 21M 时**——蓝方说"异常检测正常"。这需要仔细区分：
   - 如果 func_test 21M 的 `.perf` 里确实有正确的 SysTick B/E 事件 → **否证**"格式错配"假说 → 问题不在 alt/standard format
   - 如果 func_test 21M "正常"只是指"没有安全网 flush"（而不是"SysTick 被正确解码为异常"）→ 可能是因为 21M 下 SysTick 中断间距（1ms）远大于 I-sync 恢复间距（1024B / 21MB/s = ~49μs），所以每次中断导致的错位在下一个中断前就被 I-sync 修复了 → **不触发栈溢出**（但 SysTick 异常事件仍然丢失）

**关键验证**：检查 `functest_84m_systick.perf` 或 `functest_nosystick.perf` 中 orbetto 报告的 SysTick B/E 事件数量。如果 21M 有 SysTick 时 B/E 数量也很低（只是没有导致栈溢出），则确认"格式错配"假说。

---

## 4. 第二假说（如果格式错配被否证）：异常包与 nibble-slip 的时间相关性

如果确认 func_test 21M 有 SysTick 时异常被正确检出（否证格式错配），则需要别的解释。

### 4.1 中断时刻与 nibble-slip 的时间相关性

Proposal 22 §13 实测发现 TRACECLK 有 **5ns 级 runt/毛刺**。这些毛刺的产生可能与 STM32 内部总线仲裁有关——**中断进入时的向量取指会触发 AHB 总线仲裁，可能影响 TRACECLK 的时钟质量**。

机制：
1. CPU 正在执行 LVGL 代码 → TRACECLK 稳定
2. SysTick 中断到达 → CPU 进入异常序列（向量表取指 + 上下文保存） → AHB 总线竞争加剧
3. AHB 竞争影响 TPIU 的时钟输出 → TRACECLK 产生瞬时 jitter/glitch
4. **恰好在 ETM 产出异常包的那几个周期**，FPGA 采样发生 nibble-slip
5. 异常包的字节被吞 → 解码器丢失异常信息

**这会解释为什么丢失率远超 random 预期**：nibble-slip 不是时间均匀分布的——它集中在中断发生时刻（因为那时信号完整性最差）。

### 4.2 验证方法

- 在 FPGA 采集端记录"nibble-slip 事件"的时间戳（利用 proposal 22 §25 的 FPGA 时间戳机制）
- 对比 ETM 流中已知的 I-sync 位置 → 看 slip 是否集中在中断时段
- 或者：用 FPGA 监测 TRACECLK 的 dwell（proposal 22 §13 的 duty probe），看 runt 事件是否与 SysTick 周期相关

---

## 5. 第三假说：TPIU formatter 的帧边界切分

蓝方在第 2 条提到"TPIU 16-byte frame 封装过程中截断或错位"。让我分析这个可能性。

### TPIU 4-bit DDR、单 stream（ETM-only）的行为

- F429 只有 ETM（无 ITM 同时输出到并口）→ TPIU formatter 在单 source 模式下 **不做 stream-ID 切换**
- 单 source 模式下 TPIU 输出 = 16 字节帧，其中 15 字节 payload + 1 字节 half-sync/ID：
  - 正常帧：15 字节 ETM 数据 + `0x_F` 或 `0x7F`（ID=stream 1）
  - 全同步帧：`0xFF...FF 0x7F`（full sync + half-sync）

**关键**：在单 source 模式下，帧边界的 ID byte（`0x_F`）不会截断 ETM 包——TPIU formatter 会等当前 ETM 包完成后再插 ID byte（或用 null byte 填充到帧边界）。

**所以 TPIU 帧边界不应该截断异常包**——除非 TPIU 的 FIFO 在高速下 overflow（但蓝方确认 FPGA FIFO 无溢出，且 TPIU 内部 FIFO overflow 会产生 sync loss 而非截断）。

**裁决：第 2 条（TPIU 截断）不太可能是根因。**

---

## 6. 第四假说：`0x7E` Exception Entry 包（data tracing mode）的误用

从 `traceDecoder_etm35.c` 源码 line ~225：

```c
// ******** EXCEPTION ENTRY PACKET *****************
if ( c == 0b01111110 )  // 0x7E
{
    /* Note this is only used on CPUs with data tracing */
    _stateChange( cpu, EV_CH_EX_ENTRY );
    retVal = TRACE_EV_MSG_RXED;
    break;
}
```

注释写得很清楚：**`0x7E` exception entry 包只在有 data tracing 的 CPU 上使用**。Cortex-M4 **没有** data tracing（ETMCCER 实证无 data comparator）。

**所以在 M4 上，异常进入只能通过 Branch Address 包末尾的 Exception Information Bytes 来传递。** 如果 mortrall 只检查 `EV_CH_EX_ENTRY`，它应该能收到——但前提是解码器正确解析了 X bit 并进入了 `TRACE_COLLECT_EXCEPTION` 状态。

**让我重新审视 alt format 的 X bit 逻辑**：

```c
case TRACE_COLLECT_BA_ALT_FORMAT:
    C = c & 0x80;
    X = ( ( !C ) && ( c & 0x40 ) );  // C=0 且 bit6=1 → 有异常
    goto terminateAddrByte;
```

在 `terminateAddrByte`：
```c
if ( ( !C ) || ( j->byteCount == 5 ) )
{
    // 包结束
    if ( ( !C ) & ( !X ) )
    {
        // 普通 branch，无异常
        newState = TRACE_IDLE;
    }
    else
    {
        // 有异常信息跟随
        _stateChange( cpu, EV_CH_EX_ENTRY );  // ← 这里报告异常进入
        newState = TRACE_COLLECT_EXCEPTION;
    }
}
```

**如果 `usingAltAddrEncode = true`（正确配置）**：alt format 的最后字节 bit6=1 表示有异常 → X=true → 进入异常收集。**这应该能工作。**

**如果 `usingAltAddrEncode = false`（错误配置）**：标准格式解析。标准格式下：

```c
case TRACE_COLLECT_BA_STD_FORMAT:
    C = ( j->byteCount < 5 ) ? c & 0x80 : c & 0x40;
    X = ( j->byteCount == 5 ) && C;
    goto terminateAddrByte;
```

标准格式下 X 只在第 5 字节时才可能为 true。**Cortex-M4 是 Thumb mode（地址 32 bit），branch address 包最多 5 字节**。但如果大多数 branch 只差几位地址（短距跳转），包只有 1-2 字节就结束了（C=0 in byte 1 or 2）。此时 `j->byteCount` 不会到 5 → **X 永远为 false** → 异常信息永远不被收集。

**但对于中断进入的 branch（从 LVGL 代码跳到向量表 0x08002f4c）**：
- 地址变化大（从 LVGL 的 0x0800xxxx 跳到 0x08002f4c）→ 需要较多字节编码
- Thumb mode alt format：每字节编码 7 bit 地址，32 bit 地址需要 ceil(32/7) = 5 字节
- 所以异常进入的 branch 包**应该是 5 字节长** → 如果用 standard format 解析，在第 5 字节时 `X = (byteCount==5) && C`

但 standard format 的第 5 字节 C 计算是 `c & 0x40`（不是 `c & 0x80`！）。而 alt format 的异常标志也在 bit 6 (`c & 0x40`)...

**等等，这里有一个微妙的兼容性**：

Standard format 第 5 字节：`C = c & 0x40; X = (byteCount==5) && C`
- 如果 ETM 发出 alt format 的第 5 字节，bit6=1（表示有异常）
- Standard format 解读：`C = 1; X = true` → **进入异常收集！**

**所以对于 5 字节长的 branch 包（中断进入），即使格式错配，X 也能被正确检出？**

不对——关键是 **alt format 和 standard format 的地址编码方式不同**，导致包长度计算可能不同。如果 alt format 下包在第 3 字节就结束了（C=0），standard format 解码器也会在第 3 字节看到 C=0 → 认为包结束 → `X = (byteCount==5) && C = false`（byteCount=3≠5）→ **不收集异常**。

**所以关键问题变成**：F429 的中断进入 branch address 包实际是几个字节？

- SysTick_Handler at 0x08002f4c
- LVGL 代码通常在 0x0800xxxx 到 0x080xxxxx 范围
- 如果当前 PC 是 0x08005000，跳到 0x08002f4c，差 ~0x2000
- Alt format Thumb mode：第一字节编码低 6 bit + C bit，后续每字节编码 7 bit + C bit
- 0x08002f4c 的 Thumb 地址 = 0x08002f4c（bit0=0 in trace），需要编码 bit[31:1]
- 如果之前的地址也是 0x0800xxxx，差异在 bit[14:0] 左右 → 需要 ceil(15/7) = 3 字节（前 6 bit + 2×7 bit）
- **所以中断进入的 branch 包可能只有 2-3 字节长**

如果包只有 3 字节（byteCount=3 时 C=0）：
- Standard format：`X = (byteCount==5) && C = false` → **不收集异常信息**
- Alt format：`X = (!C) && (c & 0x40)` → 如果 bit6=1 → `X = true` → **收集异常信息**

**结论确认：如果 `usingAltAddrEncode` 被错误地设为 false，而 F429 ETM 发出 alt format 的异常包（bit6=1 in 最后字节），解码器用 standard format 解析 → X 在 byteCount<5 时永远为 false → 异常信息不被收集 → EV_CH_EX_ENTRY 不被报告。**

---

## 7. 最终诊断：`usingAltAddrEncode` 配置问题

### 检查方法

1. 在 orbuculum/orbetto 的初始化代码中搜索 `usingAltAddrEncode` 的赋值
2. 确认它是否根据 ETMCR 的 bit[21]（Branch Broadcast Mode）或 ETMIDR（ETM ID Register）来判断是否使用 alt format
3. DDI0440C §2.3.1："The ETM-M4 uses the alternative branch packet address encoding format"——这是硬编码的，不是可配置的

### 如果确认 `usingAltAddrEncode = false`（错误）

**修复极简**：在 ETM35 解码器初始化时强制 `j->usingAltAddrEncode = true`（对所有 M4 ETM），或者在 mortrall 的 `_init()` 中设置。

**预期效果**：
- 所有异常进入的 Branch Address 包的 X bit 被正确识别
- EV_CH_EX_ENTRY 正常产出
- mortrall 正确切栈
- 安全网 flush 消失
- SysTick B/E 事件从 2 → ~47（或接近预期值，减去 2.91% random 损失）

---

## 8. 如果格式假说不成立：备选根因

如果确认 `usingAltAddrEncode` 已经正确为 true，则回到"nibble-slip 与中断时刻时间相关"（§4）。此时的验证方法：

1. **Hex dump ETM 字节流**：在 orbetto 输入侧（TPIU 去帧后、送入解码器前）dump raw bytes，手动搜索 SysTick_Handler 地址（0x08002f4c → thumb address encode → 搜索 branch packet 的特征字节）
2. **在解码器里加计数器**：
   - 每次进入 `TRACE_COLLECT_BA_ALT_FORMAT` 或 `TRACE_COLLECT_BA_STD_FORMAT` 时计数
   - 每次 X=true 时计数
   - 每次成功报告 `EV_CH_EX_ENTRY` 时计数
   - 如果"进入 address collection"次数很多但 X=true 次数很少 → 确认是 X bit 解析问题
3. **在解码器里加"目标地址 watchpoint"**：当解出的地址落在 `[SysTick_Handler ± 16]` 范围时特殊打印 → 看这些 branch 是否有 X bit / Exception Info

---

## 9. 关于 2.91% unknown 本身

蓝方在 r23 评审时已接受：1.7-2.9% unknown 的主要来源是 SI/jitter/nibble-slip 本底，不是相位偏差。

**但在中断重建的语境下，这 2.9% 仍然重要**——即使修好了 `usingAltAddrEncode`，仍会有 ~5-6% 的异常包因为 random nibble-slip 被吞。**这对 47 个 SysTick 意味着 ~2-3 个丢失**（从 2/47 → ~44/47），应该可以接受。

**更重要的是**：即使偶尔丢失一个异常进入事件，mortrall 的安全网（depth flush）应该只是把那一段当作"普通函数调用"压栈 → 最终 return 回来时栈弹出 → 不应该无限累积。

**除非**：中断 ISR 内部调用了函数（SysTick_Handler 里有 LVGL tick 更新等调用），然后 ISR 返回 → PC 跳回中断前地址 → 这个地址不在当前栈顶函数的地址范围内 → 解码器认为是新的 branch → 压更多栈... → 累积。

**这是真实的级联效应**：一次异常进入丢失 → ISR 内的所有调用被压到主栈 → ISR 返回后地址不匹配 → 持续错位 → 直到 I-sync 重锚或安全网 flush。**每个丢失的中断导致 ~数十个错误事件**（ISR 执行的指令 + 返回后的错位），所以 47 个丢失中断中每个都产生 ~10-20 个错误 → 总共 ~500-1000 个错误事件分散在 56K 事件中 ≈ ~1-2%。这和观测到的"每 1ms flush"吻合。

---

## 10. 总结与建议

### 最可能根因（优先级排序）

| 优先级 | 假说 | 验证成本 | 预期修复难度 |
|:---:|------|:---:|:---:|
| **1** | `usingAltAddrEncode` 未正确设为 true，导致 X bit 在短 branch 包中永远 false | 5 分钟（检查代码） | 1 行代码 |
| 2 | Alt format 下 exception info byte 的解析有 off-by-one（在特定 byteCount 下 X 计算有 bug） | 30 分钟（构造测试向量） | 中 |
| 3 | nibble-slip 与中断时刻时间相关（SI 问题） | 需硬件（FPGA 加 slip 时间戳） | 无纯软件修复 |
| 4 | mortrall 的 `_isLegitExceptionTarget` 门控误拒 | 10 分钟（加打印） | 低 |
| 5 | TPIU 帧边界截断 | 不太可能 | — |

### 建议操作

1. **立即做**：检查 orbetto/mortrall 初始化中 `usingAltAddrEncode` 的值。如果 false → 改 true → 重测。
2. **同时做**：在解码器入口加一个"地址=SysTick_Handler 时打印 X 值"的调试点，确认异常包是否到达了解码器且 X 是否被正确计算。
3. **如果 #1 修好了问题**：记录为 "ETM3.5 decoder M4 alt-format misconfiguration"，提一个 orbuculum upstream issue。
4. **如果 #1 没解决**：用 hex dump 方法手动验证 ETM 字节流中是否存在异常信息字节（在 SysTick 地址的 branch 包后）。

---

## 附：对蓝方五条假说的逐一评判

| 蓝方假说 | 红方评判 |
|----------|----------|
| 1. ETM 硬件层未产出异常包 | **不太可能**。ETM-M4 的异常跟踪是基本功能，不需要 data trace 支持。除非 ETMCR 配置有误（需核查 ETMTEEVR 是否禁了异常事件），但不太可能。 |
| 2. TPIU 截断 | **不太可能**（§5 分析）。单 source 模式下 TPIU 不会截断。 |
| 3. FPGA nibble-slip 随机吞 | **不足以解释**。概率分析正确——2.91% 不能解释 95.7%。但 slip 可能与中断时刻时间相关（§4），作为第二假说保留。 |
| 4. 解码器对齐错位 | **最可能**——但精确机制是 `usingAltAddrEncode` 配置错误导致 X bit 永远为 false（§6-7），而非 nibble-slip 导致的持续错位。 |
| 5. mortrall 门控拒绝 | **可能但次要**。即使地址有 1 bit 错被拒，不应该 95.7% 全拒。作为第四假说保留。 |

---

## 红方立场

**这是一个解码器配置/逻辑 bug，不是物理层问题，不是架构困境。** 修复成本可能极低（1 行代码改 `usingAltAddrEncode`），最坏情况需要在解码器里修正 alt-format 异常字节的解析逻辑。

蓝方不需要在物理层（PSINCDEC/相位/SI）做任何改动来解决这个问题。**如果修好解码器后 SysTick 恢复正常，则 84M 满速 LVGL trace 的中断重建困境不存在——它从头到尾就是一个解码器 bug。**

建议蓝方先花 5 分钟查 `usingAltAddrEncode` 的值，再决定下一步。
