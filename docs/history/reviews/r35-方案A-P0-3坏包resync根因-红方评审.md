# r35 — proposal 43 §18（P0-3 "坏包不 resync" 根因 + 方向 C）红方评审

**日期**：2026-08-02
**对象**：`proposals/43` §18（P0-3 结论：真凶=坏包后不 resync），旁及 §13/§16/§17
**复核**：`mortrall.hpp`（_traceCB 318-560、EV_CH_ADDRESS 476-527、self-check 335-360/1666-1731）、`traceDecoder_etm4.c`（180-195 overflow、403 TraceOn、409 EAM、516 resync、867 overflow packet）
**立场**：严格证伪。惯犯：p42 §2.5、r32 E2、p43 A2、p43 §13"批边界"——全是"没坐实就命名根因"。对每条问：实测 vs 推断？

---

## 一句话裁决

**§18 比前几轮扎实——它自己推翻了 §13 的"批边界"（32% 数字实测）、当场证伪了 overflow 假设（overflows=0 实测）、apples-to-apples 用指令数把 §15 的"漏 atom"反转成 §16 的"过度行走 4.6×"（实测）。这些是真进步。但它落地的新根因"mortrall 不处理坏包故不 resync"有一个致命的推断漏洞：我读了代码，mortrall 在 `EV_CH_ADDRESS`（line 476-527）里每次都做 `workingAddr = cpu->addr` 重锚——它有隐式 resync 路径。所以"没写 EV_CH_TRACESTART handler ⇒ 一定不 resync"是伪推断（r34 Q 早就预判的"重锚逻辑本该接住却没接住"）。真凶到底是"完全没 resync"还是"ADDRESS 重锚了 workingAddr 但没重置 disposition/incAddr、或重锚来得太晚（坏包后的 atom 批在 ADDRESS 到达前已被走完）"——§18 没区分，方向 C（处理 TRACESTART 重置 workingAddr/disposition）建立在未证的前半上。方向 C 继续冻结，补 P0-3b。**

---

## 逐条判定

### 主张 1：推翻"批边界"——6698 次里只 32% 在 incAddr==0 🟩 **成立（实测）**
self-check 全量分类 6698 次 iBR-not-taken，68% 发生在批内还有 atom 时。**这是实测，且诚实推翻了自己 §13 的结论**（§13 只看 pop@a2f2 一个点）。方法论上是正确的自我纠错——r34 Q2 判"批边界未证"，§18 用全量数据证明批边界只解释 32%，接受红方并超越。

**但对红方 Q 的子问必须答**：
- **incAddr 语义**：核代码 line 546 `incAddr = cpu->eatoms + cpu->natoms`（移位**前**的总数），line 630 `incAddr--` 与 `disposition >>= 1` 同步递减。所以"incAddr==0"= 本批 atom 已走完。§18 用 incAddr==0 判"批耗尽"**语义正确**（🟩）。
- **但"68% 批内失败是独立证据"存疑 🟡**：68% 批内 not-taken 很可能**本身就是上游 workingAddr 漂移的下游结果**——workingAddr 漂到错位置后，在错误的指令上消费本批 atom，pop/bx 落在错 atom 上判 not-taken。这不能独立证明"根因不是批边界"，只能证明"批边界不是唯一表现"。§18 用它推翻批边界方向对，但不能反过来当"坏包 resync"的正证。

### 主张 2：99.3% 无条件返回、0 个条件间接分支 🟡 **部分成立，分类可靠性有隐患**
**实测部分**：触发集 92 地址逐一反汇编，6652 次是 bx lr/pop{pc}/ldm..pc/ldr pc。

**红方 Q（分类可靠性）命中一个真实风险**：§14.4 蓝方自己血泪记过"IT 块内条件指令助记符不带 cc 后缀"。§18.1 的分类是"对 92 个地址逐一反汇编分类"——**如果用 objdump 助记符判"无条件"，IT 块内的条件 bx/pop 会被误归为无条件**。§18 没说清分类用的是 objdump 助记符还是 capstone `detail->arm.cc == ARM_CC_AL`。
- **若用助记符** → "0 个条件间接分支"可能是**分类工具的盲区**造成的假象，不是真的没有。
- **判据**：§18.1 必须声明分类方法。若是助记符，需用 capstone cc 位重分类那 92 个地址，确认没有一个在 IT 块内。

**"触发集恰好 0 个条件间接分支"为何可疑**：如果真凶是 workingAddr 漂移（主张 4），漂移后 workingAddr 落在**任意**指令上，pop/bx 只是恰好是漂移落点附近最常见的 iBR 类型——"全是无条件返回"可能只是**反汇编落点的统计分布**，不是"失败只发生在无条件返回"。这削弱了"无条件返回不该 not-taken ⇒ 必是 resync 问题"的推断力度（和 r33 §1.5 犯的是同类推断）。

### 主张 3：overflows=0，overflow 假设证伪 🟩 **成立（实测，且方法诚实）**
- self-check 读 `changeRecord` 位统计 overflow，得 0。
- 核对 `traceDecoder_etm4.c:190` `if(c==0x05 && asyncCount==1) cpu->overflows++` + line 867 overflow packet → EV_CH_OVERFLOW。overflow 计数机制真实存在。
- **"0 - 205" 读法**：核 `mortrall.hpp:282` `printf("Overflows: %llu - %llu", cpu->overflows, cpu->ASyncs)` —— **第一个数确实是 overflows、第二个是 ASyncs**。§18.2 说"0=溢出、205=A-sync"**读对了**（并更正了 §24 之前读反）。🟩
- **接受这条证伪**：overflow 不是根因，实测坐实。

### 主张 4（核心）：真凶=坏包后不 resync，mortrall 不处理 EV_CH_TRACESTART/OVERFLOW/DISCARD 🟥 **前半实测、后半致命推断漏洞**

**实测部分（可信）**：
- opencsd 对同段 etm.bin：340 I_BAD_SEQUENCE、303 I_TRACE_INFO、806 ADDR_NACC，最终恢复到 98091 range（≈721K 指令）。**这证明流里确有坏包，且 opencsd 靠 A-sync/Trace-Info 能恢复。**🟩
- mortrall 3.33M 指令 vs opencsd 721K = 4.6×（§16 实测，apples-to-apples 用指令数，方法正确）。🟩
- 坏包非丢包（seq-gap≈0、lost_cnt=0、overflows=0），来自采集字节质量。🟩

**致命推断漏洞（🟥，这是本次评审的核心打击）**：

§18.3 说"mortrall 对 I_BAD_SEQUENCE/EV_CH_TRACESTART/NACC 全不处理（grep 0 引用），坏包后不 resync，带着上一段 workingAddr/disposition 继续走"。

**我读了代码，这个因果链断了**：mortrall 在 `EV_CH_ADDRESS` 处理器（`mortrall.hpp:476-527`）里，**line 524 每次都执行 `Mortrall::r->op.workingAddr = cpu->addr`**——这是一条**隐式 resync 路径**。而 `traceDecoder_etm4.c` 里：
- Trace On（line 403）→ `EV_CH_TRACESTART`，且 ETM4 spec 里 Trace On **后面必跟 address 包**；
- Exact Match Address（line 409）、Short/Long Address 包 → `EV_CH_ADDRESS`。

**所以坏包 resync 后 orbuculum 解码器发出的 address 包，mortrall 的 line 476 handler 会消费并重锚 workingAddr。** "没有 EV_CH_TRACESTART handler" **不等于** "不 resync"——mortrall 靠**下一个 ADDRESS 包**隐式重锚，正是 r34 预判的"另一条隐式 resync 路径（EV_CH_ADDRESS 重锚）本该接住"。

**§18 把"没写 TRACESTART handler"直接等同于"不 resync"，是 grep 出"0 引用"后的推断，不是单步观察 mortrall 在坏包处的实际行为。** 这是 p42→r32→r33→§13 之后**第五次同型复发**：看一个静态信号（这次是"grep 无 handler"）就命名根因，没做动态单步。

**真正未区分的三个假设**：
1. **完全不 resync**：坏包后到下一个 ADDRESS 包之间，mortrall 带旧 workingAddr 走了 N 个 atom 批 → 漂移。
2. **重锚了但太晚**：mortrall 在 ADDRESS 包处确实重锚 workingAddr，但坏包**后、ADDRESS 包前**的那批 atom 已经用错的 workingAddr 走完了（因为 atom 批在 ADDRESS 之前到达）——这是"重锚时机/顺序"问题，不是"没 resync"。
3. **重锚 workingAddr 但没重置 disposition/incAddr**：核代码——`incAddr`/`disposition` 是 `_traceCB` 的**局部变量**（line 328-329），每次 EV_CH_ENATOMS 从 `cpu->disposition` 重载（line 546-547），**不跨 callback 残留**。所以 §18.3/§18.4 说"带着上一段的 disposition 继续"**对 disposition 是错的**（它每批重载），只有 **workingAddr（`op.workingAddr` 持久）** 可能带病。这进一步说明 §18 对机制的描述不精确。

**方向 C（处理 EV_CH_TRACESTART，重置 workingAddr/disposition）建立在假设 1 上**。如果真相是假设 2（重锚太晚），处理 TRACESTART 也救不了（问题在 ADDRESS 与 atom 批的到达顺序）；如果是假设 3 的变体，重置 disposition 是 no-op（它本就每批重载）。**方向 C 没坐实假设 1 就是"没坐实就动手"的又一次。**

### 主张 5：opencsd 340 坏包与 mortrall 6698 失败位置对应 🟥 **未证**
§18 只证明了"两者都在坏包区域附近"（第一次漂移点 0x8008eb4 附近 opencsd 有 BAD_SEQUENCE）。**没证明**：
- 6698 次 mortrall 失败**每一次**都由一个 opencsd 坏包触发（1:1）；
- 340 坏包**每一个**都触发了 mortrall 失败。
- 340 vs 6698 数量差 **20 倍**——如果一对一，一个坏包要引发 ~20 次失败，那正是"漂移级联"的说法，但**级联假设本身没被单步证实**，只是数字凑出来的推断。
- **必须做**：把 opencsd 的 340 个 BAD_SEQUENCE 字节偏移与 mortrall 6698 次失败的字节偏移**逐一对齐**，画出"每个坏包 → 引发几次失败 → 到下一个 ADDRESS 包重锚为止"的区间。这才能区分假设 1/2。

### 主张 6：方向 C 风险（吞合法首包 / 密集坏包区反复重锚丢覆盖，重蹈 A2）🟡 **红方担忧成立，§18 未回应**
§18.5 自己说"需红方评审确认 resync 语义不会吞掉正常 Trace On 后的合法首包"——这个担忧是实的：
- Trace On 后的第一个 address 包是**合法重锚点**，若方向 C 在 TRACESTART 时清空 disposition/incAddr，正好清掉的是即将到来的合法批 → 可能丢真实覆盖（cardinality 掉，重蹈 A2 的 -77）。
- 密集坏包区（340 个坏包若聚集）反复重锚 → 反复丢弃 atom 批 → cardinality 掉。
- **方向 C 必须先在 P0-3b 用 dry-run（只统计"若重置会丢多少 atom 批"，不真改行为）评估 cardinality 影响**，再决定是否动手。

---

## 方向 C 动手前必须补的实验（P0-3b，可证伪、判据明确、能唯一区分三假设）

**在 P0-3b 唯一确定"完全不 resync"vs"重锚太晚"vs"第三机制"之前，方向 C 继续冻结、禁止写修复代码（延续 r34 纪律）。**

### P0-3b-1 🔴 单步 mortrall 在第一次漂移点，看 workingAddr 到底有没有被 ADDRESS 重锚
- **做法**：在 `mortrall.hpp:524`（`op.workingAddr = cpu->addr`）打点，dump 第一次漂移（0x8008eb4）**之后**到假重入之间**每一个** EV_CH_ADDRESS 事件的 `(旧 workingAddr, cpu->addr, 两者是否相等, 距上次漂移的 atom 批数)`。
- **判据（唯一区分三假设）**：
  - 漂移后**长时间没有 EV_CH_ADDRESS**（mortrall 连走 N 批 atom 无重锚）→ **假设 1（不 resync）成立** → 方向 C（补 resync）方向对。
  - 漂移后**有 EV_CH_ADDRESS 但 workingAddr 已经走飞、重锚把它拉回却为时已晚**（中间的 atom 批已污染 cardinality）→ **假设 2（重锚太晚）** → 方向 C 无效，要改的是"坏包后暂停走 atom 直到 ADDRESS 到达"。
  - 漂移后 EV_CH_ADDRESS 重锚了 workingAddr 但下一批 atom 仍从错误 disposition 走 → 假设 3 → 查 disposition 重载逻辑。

### P0-3b-2 🔴 坏包↔失败 1:1 对齐（证主张 5）
- **做法**：给 orbuculum 解码器在 emit BAD_SEQUENCE/TRACE_INFO/TRACESTART 时打字节偏移；给 mortrall 每次 iBR-not-taken 打字节偏移。**离线 diff 两个偏移序列**。
- **判据**：
  - 每次 mortrall 失败都能对到"上一个坏包之后、下一个 ADDRESS 之前"的区间 → 坏包驱动成立。
  - 存在**远离任何坏包**的 mortrall 失败 → 还有第二机制，方向 C 不完整。

### P0-3b-3 🟡 方向 C 的 dry-run cardinality 影响预估（防重蹈 A2）
- **做法**：selfcheck 模式下模拟"每次 TRACESTART/坏包重置 disposition/incAddr"会丢弃多少 atom 批、覆盖多少 PC，**只统计不改行为**。
- **判据**：预估 cardinality 损失 > 0 → 方向 C 会重蹈 A2，需改成"重锚 workingAddr 但保留即将到来的合法批"。

### P0-3b-4 🟡 分类方法核实（证主张 2）
- 用 capstone `detail->arm.cc` 重分类 §18.1 的 92 个触发地址，确认"0 个条件间接分支"不是助记符盲区造成的假象。

---

## 逐条判定汇总

| 主张 | 判定 | 实测/推断 |
|---|---|---|
| 1 推翻批边界（32%） | 🟩成立 | 实测全量；但"68%批内"不能当 resync 正证（🟡下游结果） |
| 2 99.3%无条件、0条件间接 | 🟡部分 | 反汇编实测；分类方法未声明，助记符盲区风险；"0条件间接"可疑 |
| 3 overflows=0 证伪 overflow | 🟩成立 | 实测，"0-205"读法核对正确 |
| 4 坏包不 resync（核心） | 🟥前半实测后半致命推断 | opencsd 340坏包=实测；"mortrall不resync"=grep推断，被 EV_CH_ADDRESS line524 重锚证伪 |
| 5 坏包↔失败位置对应 | 🟥未证 | 只证"都在附近"；340 vs 6698 的20×级联未单步证实 |
| 6 方向C风险（吞首包/丢覆盖） | 🟡担忧成立 | §18 自己提出但未评估，需 dry-run |

**§18 相较前几轮的真进步（公允标注）**：自我推翻 §13 批边界（诚实）、当场证伪 overflow（先试后信）、§15→§16 用指令数反转"漏 atom"→"过度行走"（apples-to-apples 方法论正确）、self-check 基建（env 门控纯诊断）。这些方向对、方法诚实，是本项目少见的"边推翻自己边前进"。

---

## 最后一句话给用户的裁决

**继续冻结方向 C，补 P0-3b-1。** §18 这轮比以前扎实——它自己推翻了"批边界"、证伪了 overflow、用指令数把"漏 atom"反转成"过度行走 4.6×"，这些都是实测硬结论，值得肯定。但它落地的新根因"mortrall 不处理坏包所以不 resync"是 grep 出"没 handler"后的推断：我读了代码，mortrall 在 `mortrall.hpp:524` 的 EV_CH_ADDRESS 处理里每次都 `workingAddr = cpu->addr` 重锚，它**有**隐式 resync 路径，坏包后 orbuculum 发的 address 包 mortrall 会消费。所以真凶到底是"完全不 resync"还是"重锚了但坏包后那批 atom 在 ADDRESS 包到达前已经用错 workingAddr 走完了"——这俩修法完全不同（前者补 TRACESTART handler，后者要暂停走 atom 等重锚），§18 没区分就选了方向 C。而且 §18 自己说 disposition 带病延续，但 disposition 是 `_traceCB` 局部变量、每批从 cpu->disposition 重载，根本不跨 callback 残留——机制描述就不准。动手前必须做 P0-3b-1：在漂移点单步，看漂移后到假重入之间 mortrall 到底有没有收到 EV_CH_ADDRESS、workingAddr 有没有被重锚、重锚是不是来晚了。这一步用数据唯一区分三假设之前，写方向 C 代码就是第五次"没坐实就动手"。
