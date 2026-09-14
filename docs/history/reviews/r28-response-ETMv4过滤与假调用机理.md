# r28 响应 — ETMv4 过滤能力与"假调用"机理（手册原文核实）

**日期**：2026-07-19
**触发**：讨论"能否只 trace BL/BLX/POP（函数进出）"及"假调用是否因感知不到 POP 返回"
**手册**：`refs/IHI0064H_b_etm_v4_architecture_specification.pdf`（转文本 `refs/ihi0064h.txt`，
下引行号均指该文本；章节名为手册原章节）
**硬件**：STM32H743 CoreSight ETM-M7（DDI0494D），寄存器读回见 §5
**立场**：每条结论附原文引用；无法从原文直接得出的标注为【推断】并说明验证方法。

---

## 结论速览

| # | 结论 | 依据 |
|---|------|------|
| C1 | ViewInst 过滤只按**指令地址**，无"按指令类型（BL/BLX/POP）过滤"维度 | §4.1.3 原文 |
| C2 | `TRCCONFIGR.BB` 只控制**直接分支**发不发地址，对间接分支无作用 | §2.4.4 / §7 原文 |
| C3 | BL / BLX\<immed\> = 直接分支；POP{PC} / BX / LDR PC = 间接分支 | Appendix F 表 F-5/F-6/F-8 |
| C4 | BB-OFF 下直接分支目标由解码器**从 ELF 反汇编盲推** | §5.x 原文 |
| C5 | 间接分支取目标时**原则上**发 Address 元素（锚点） | §5.x 原文 |
| C6 | **例外**：返回栈使能时，返回等间接分支**不发 Address 元素**，解码器从返回栈 pop 目标 | §5.3.4 原文 |
| C7 | 解码器**无条件**维护 15 深返回栈（即使 ETM 侧 RS=0） | §5.3.4 原文 |
| C8 | 本 M7：返回栈**已实现**（TRCIDR0.RETSTACK=1），当前配置 ETM 侧 **RS=0** | 硬件读回 |

---

## C1 — ViewInst 过滤只按地址，没有"指令类型"维度

**问题**：能不能让 ETM 只发 BL/BLX/POP（即"只 trace 函数进出"）？

**原文**（§4.1.3 The instruction-based filtering model，line 9930-9932）：
> "instruction tracing can be made active or inactive **based on instruction addresses**. The
> ViewInst function provides this functionality."

同节列出 ViewInst 的**全部**能力（line 9934-9960，改述）：
- 在一条指令地址**开始**、另一条地址**停止** trace（Start/Stop）；
- **包含/排除指令地址区间**（include/exclude address ranges）；
- 基于 enabling event（资源事件）开关；
- 按 Exception level 禁止。

**判定**：ViewInst 过滤的输入永远是"指令**在哪个地址**"，**没有任何一处允许按"指令是什么类型"
过滤**。故"只发 BL/BLX/POP"这个愿望在 ETMv4 架构层**不存在对应开关**——只能用 BB-OFF（C2）
让间接分支/返回天然成为地址锚点去**逼近**，而非精确实现。

（补充：本 M7 连按地址区间过滤都做不到——`TRCIDR4.NUMACPAIRS=0`，见
`r28-response-候选ab硬件不可用.md`。）

---

## C2 — BB 只控制直接分支的地址广播

**原文**（§2.4.4 Branch broadcasting，line 6871-6872）：
> "it can be programmed to explicitly trace the **target addresses of direct branch and ISB
> instructions**."

**原文**（line 7117-7119）：
> "Whether an implementation supports branch broadcasting is IMPLEMENTATION DEFINED. If it does,
> the trace unit can be programmed so that it explicitly traces the target addresses of **direct
> branch and ISB instructions** that the PE [executes]."

**判定**：BB 的作用域**仅限直接分支（+ISB）**。它对间接分支的地址输出**没有任何影响**——
间接分支的地址处理另有规则（C5/C6）。所以 BB=1↔BB=0 的差异 = "每个直接分支发/不发地址"。

---

## C3 — 指令分类：调用是直接分支，返回是间接分支（Appendix F）

**直接分支**（Table F-5，T32 32-bit direct branches）：
> `B`, `B<cc>`, **`BL` (Branch with Link)**, **`BLX <immed>` (Branch with Link and Exchange)**, `ISB` …

**间接分支**（Table F-6 / F-8，T32 indirect branches）：
> `LDR to the PC`, `LDM including the PC`, `TBB/TBH`, **`BX`**, `BXNS`, `ADD/MOV to the PC`,
> **`POP including the PC` (Pop from the stack including the PC)** …

**判定**：
- **函数调用**（`BL`/`BLX immed`）几乎都是**直接分支**——目标是编译期立即数 → 受 BB 控制。
- **函数返回**（`POP {…,PC}`/`BX LR`）是**间接分支**——目标在栈/寄存器 → 不受 BB 控制，走 C5/C6。

这解释了为什么"BB-OFF 只 trace 函数进出"这个直觉部分成立（返回是间接、天然发地址锚点）
但不完整（调用是直接分支、BB-OFF 下不发地址、靠盲推 C4）。

---

## C4 — BB-OFF 下直接分支目标由解码器从 ELF 盲推

**原文**（§5.x Atom instruction trace element，line 16945-16948）：
> "For **direct branch** and ISB instructions, a trace analyzer must **infer the target address**
> and instruction set of Atom elements **from the instruction opcode in the program image**. If
> the direct branch or ISB is from a branch broadcast region, the trace analyzer does not need to
> infer the target address … because this is explicitly traced using an Address element."

**判定**：BB-OFF（非 broadcast region）时，直接分支**只发一个 atom（E/N 方向）**，目标地址由
解码器读 ELF（program image）自己算。这就是"盲推"的架构定义。BB=1（broadcast region）则直接
分支也发 Address 元素，无需盲推。

---

## C5 — 间接分支原则上发 Address 元素（锚点）

**原文**（§5.x Address element，line 16179-16182）：
> Address 元素 "is generated when an **indirect branch** is taken or when an exception occurs or
> after a Q element is generated."

**判定**：间接分支（含返回）取目标时**原则上**输出 Address 元素，成为解码器的地址锚点。
这是"返回本应是可靠锚点"的架构依据——但有 C6 的重大例外。

---

## C6 — 返回栈例外：返回不发 Address 元素，解码器从返回栈 pop 目标

**这是"感知不到 POP 返回"直觉的精确机制。**

**原文**（§5.3.4 Operation of the trace analyzer return stack，line 19614-19616）：
> "The purpose of the trace analyzer return stack is to provide target addresses for **indirect
> branch instructions that are traced without a target address**, that is, to provide an address
> when an indirect branch instruction is **traced without an Address element**."

**原文**（line 19621-19623）：
> "If the trace unit indicates that the PE has taken an **indirect branch**, but it **does not
> output an Address element** before the next Atom element, Q element, or Exception element … the
> top entry of the … return stack is **popped and the value that it contains is used as the
> target address**."

**判定**：返回栈生效时，函数返回（`POP{PC}`/`BX LR`）若目标是"调用点下一条"（常态），ETM
**只发一个 atom、不发 Address 元素**（省一个地址包的带宽），解码器从返回栈 pop 目标。
**后果：返回退化为"atom + 猜目标"，不再是校正 PC 的外部地址锚点。** 若此前直接分支盲推（C4）
已把 PC 带偏，返回栈里 push 的是**错误函数**的返回地址，pop 出来继续错，**锚点自我污染、无外部
纠正**——直到某个真正发 Address 的间接分支才拉回。这正是 `core_list_init→HAL_UART_Init ×22`
持续多次的成因链。

---

## C7 — 解码器无条件维护返回栈（即使 ETM 侧 RS=0）

**原文**（§5.3.4，line 19611-19612）：
> "the trace analyzer **must implement a return stack with a depth of 15 entries**."

**判定**：这是对 trace analyzer（解码器，如 OpenCSD）的**强制要求**，与 ETM 侧
`TRCCONFIGR.RS` 是否置位无关。故即使我们没显式开 ETM 返回栈（C8, RS=0），OpenCSD 仍会对
"未发 Address 元素的间接分支"用返回栈补目标。**盲推链上的返回不构成可靠外部锚点这一点成立。**

**【推断，待验证】**：ETM 侧 `RS=0`（C8）时，返回究竟是"仍发 Address 元素"（则返回是锚点，
假调用主因纯为 C4 直接分支盲推越界）还是"已省略、走解码器返回栈"（则 C6 返回栈补错也参与）。
原文（C6）描述的是"trace unit 不发 Address 元素时"的解码器行为，但**是否不发**取决于 ETM 侧
返回栈状态与实现。**验证方法**：解 BB-OFF+cache 流时开 OpenCSD packet-level 日志
（`trc_pkt_lister -decode` 的包级输出），核对 `HAL_UART_Init×22` 区间内的返回是伴随
`I_ADDR_*` 包（真锚点）还是无地址包（走返回栈）。这决定假调用主因是"纯 C4 盲推越界"还是
"C4 + C6 返回栈补错"。

---

## C8 — 本 M7 硬件读回

```
TRCIDR0     = 0x080006e1   -> bit9 RETSTACK = 1  (返回栈已实现)
TRCCONFIGR  = 0x00000009   -> bit0 INSTP0=1, bit3 BB=1(读时), bit12 RS = 0 (ETM侧返回栈未开)
TRCIDR4     = 0x00114000   -> bit[3:0] NUMACPAIRS = 0 (无地址比较器, 见候选ab文档)
TRCIDR3     = 0x07090004   -> bit25 SYNCPR = 1 (A-sync周期固定1024B, 见候选ab文档)
```

**原文**（§7 TRCCONFIGR.RS, bit[12]，line 35755-35762）：
> "RS, bit[12] Return stack enable bit: 0 Return stack is disabled. 1 Return stack is enabled.
> TRCIDR0.RETSTACK indicates whether this bit is supported."

---

## 对方案的意义（收束到 r28 主线）

1. **"只抓 BL/BLX/POP"硬件不可达**（C1 无类型过滤 + `NUMACPAIRS=0` 无地址区间过滤）。只能 BB-OFF
   逼近，代价是直接分支盲推（C4）。
2. **假调用根因是 ETM 压缩模型 × cache 稀疏锚点的固有解码局限**，非采集丢字节（R1 已双证）：
   直接分支目标不发（C4）+ 返回不构成可靠锚点（C6/C7）→ cache 拉稀锚点后盲推越界进相邻函数。
3. **缩盲推跨度的两个片上手段（range-filter/加密 A-sync）硬件都不可用**（候选 ab 文档），
   叠加本文 C1，**唯一剩余可行路线 = 解码器侧约束盲推 或 采样式统计**（与 R2 定位一致）。
4. **待验证的 C7【推断】**（返回栈补错是否参与）是下一步可做的零上板实验，锁死假调用主因。
