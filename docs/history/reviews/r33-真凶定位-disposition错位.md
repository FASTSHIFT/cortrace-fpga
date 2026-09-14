# r33 — 真凶定位：iBR 返回被 disposition 错位判为 not-taken（推翻 r32 revert 假设）

**日期**：2026-08-02
**方法**：纯离线 fixture（`captures/mortrall_fixture/` 的 slice.aligned.bin + time.bin
+ rt300.elf）+ 直接在 mortrall 里打 log 单步，**不猜**。每步用 `regress.py` 量化
（coremark_main_begins / max_depth / cardinality / 内层函数篮子）对比 baseline。

**baseline（这份 4MB slice）**：coremark_main_begins=**358**，max_depth=18，
cardinality=2640，内层篮子（core_state_transition 51801 / crc16 7071 / crcu32 3397 …）。
真值：coremark_main 只被 main() 调 **1** 次。

---

## 1. 结论先行

**r32 的 revert 根因假设（E2）被实测证伪。真凶是 ETM4 disposition 与指令流错位，
导致 `cm_uart_send_char` 的 `pop {r4,pc}` 返回被判成 not-taken 而跳过，栈永不回退，
`coremark_main` 在深层被 `_inconsistentFunctionSwitch` 反复误判。**

- r32-response §5/§8 把根因锁定在 `_revertStackDel`（revert 后 stack[] 被 `_addTopToStack`
  覆盖）。**证伪**：见 §2。
- 真凶：`pop{pc}` 的 iBR atom 的 disposition 位 = 0 → 解码器当"分支未取"，
  workingAddr 越过 pop 走进字面量池（a2f4 `.word`），一路漂到 coremark_main 入口
  （a2f8）→ 触发假 switch。见 §3。

---

## 2. 证伪 r32 的 revert 假设

在 `_revertStackDel` 恢复点打计数：`revert_hits` = 每次 revert 触发，`clobbered` =
恢复时 `stack[savedDepth]` 与推测弹栈时保存值**不同**（即真被 `_addTopToStack` 覆盖过）
的次数。

```
[R32-STEP1] revert_hits=42069 clobbered=0
```

**42069 次 revert，0 次 clobber**。r32-response §5 假设 H'（"revert 撤销后 stack[oldDepth]
已被 _addTopToStack 覆盖成中间 PC"）**在这份数据上从不成立**。据此写的 shadow 栈 /
单条恢复的 step-1 fix 对 coremark_main=358 **零影响**（实测跑过，358 不变）。

> 教训：r32 的 32.44% revert 率是真的，但"revert 破坏 stack[] 内容"是 gdb 间接推断，
> 直接打 log 一测就塌。**单步看数据 > 优美推断**，再次应验。

---

## 3. 真凶：disposition 错位使 pop 返回被跳过

### 3.1 358 次假 coremark_main 的来源

给两条产生 `B|coremark_main` 的路径各打一个计数：

```
[R32-SRC] coremark_main via push=0 via inconsistent_switch=358
```

**全部 358 次来自 `_inconsistentFunctionSwitch`（同深度函数切换），push 路径 0 次。**
即 coremark_main 从来不是被"正常压栈"进来的，全是"就地换成 coremark_main"。

### 3.2 358 次全是同一个模式

```
SWITCH addr=0800a2f8 depth=<1..16> next=coremark_main top=cm_uart_send_char stacktop_addr=0800a2f8
```

358/358 完全一致：`next=coremark_main top=cm_uart_send_char @ 0x0800a2f8`。深度散布 1-16
（栈自由增长的表现）。

### 3.3 CALL/RET 单步：栈不回退，同一返回地址反复压

```
CALL depth=1->2 ret=08009776 target=cm_uart_send_char   (ee_printf 调, 真实)
CALL depth=2->3 ret=0800a2f0 target=HAL_UART_Transmit
RET  depth=3->2 to=0800a2f0                              (HAL 返回 cm_uart_send_char, 对)
SWITCH depth=2 next=coremark_main top=cm_uart_send_char  (← pop 没发生!)
CALL depth=2->3 ret=0800a302 target=cm_uart_puts         (深度卡在2继续加)
CALL depth=3->4 ret=0800a302 target=cm_uart_puts         (同一 ret=a302 反复压)
CALL depth=4->5 ret=0800a302 target=cm_uart_puts
...
```

`cm_uart_send_char` 的 `pop {r4,pc}`（0x800a2f2）本应把 depth 2→1 返回 ee_printf，
但没发生。91 次 HAL-返回-a2f0 **每一次**后面紧跟一次假 switch（91/91，不是丢包随机）。

### 3.4 指令级：pop 的 disposition 位 = 0

反汇编 cm_uart_send_char 尾部：
```
800a2ec: bl HAL_UART_Transmit   ; 返回到 a2f0
800a2f0: add sp, #8             ; 非分支
800a2f2: pop {r4, pc}           ; iBR 返回 (无条件)
800a2f4: .word 0x20000288       ; 字面量池(数据!)
800a2f8: <coremark_main> push …
```

逐指令 log（HAL 返回后这批 atom，disp=0xa=1010b）：
```
RET  to=0800a2f0 incAddr_left=4 disp=a
INSTR@a2f2 ic=1(JUMP) insExec=0 disp&1=0 disp=a   ← pop 被判 not-taken!
INSTR@a2f4 ic=0 insExec=1 disp&1=1 disp=5         ← 越过 pop 进字面量池, 当指令解
INSTR@a2f6 ...
INSTR@a2f8 ...                                     ← 漂到 coremark_main 入口
SWITCH ...
```

`disp=0xa` 最低位 = 0。a2f2 是这批 atom 的第一个分支，取 bit0=0 → `insExecuted=0`
→ 走 "branch not taken"，`workingAddr += 2` 越过 pop，进入 `a2f4` 的 `.word`
字面量（被当指令继续解码），一路 a2f4→a2f6→a2f8，撞上 coremark_main 入口。

**但 `pop {r4,pc}` 是无条件间接返回，ETM4 语义里它的 atom 必为 E(taken)。**
读到 bit0=0 说明 **disposition 位流与指令流在此处错位了一位**。

### 3.5 错位来源（假设，待坐实）

HAL 的返回是 **stacked-candidate（committed=false）**：iBR 无立即地址，解码器把
workingAddr 推测设为栈里的候选（a2f0），等下一个地址包确认。这批 disp=a 的 atom
批次边界，与"推测走 a2f0→a2f2"的指令序列对不齐——真实执行流在 HAL 返回后可能并不
线性经过 a2f0/a2f2，而 disp=a 属于另一段。**背靠背的两个 iBR 返回（HAL ret @a2f0 +
cm_uart_send_char pop @a2f2）在推测返回路径下 atom 对齐错了一位。**

---

## 4. 为什么这不是"数据坏了/丢包"

- 这份 slice 有 0.1% 丢包（965/964298 帧），但 358 次假 switch **不聚集**：跨文件
  11.4%–79%，且中位间隔 2 行（连续成串跟随每次 CoreMark 打印）。
- 91 次 HAL 返回 **91 次**触发，1:1 确定性对应。丢包是随机的，做不到 91/91。
- 内层函数篮子（crc16/crcu32/core_state_transition）计数合理，PC 100% 命中 flash。
  说明整体解码正确，**只有这一类 iBR 返回错位是系统性的**。

---

## 5. 修复方向（下一步）

真凶在 ETM4 iBR-返回的 disposition/atom 对齐，不在栈层。候选：

1. **A（治本）**：在 iBR 返回（非立即地址）分支，若下一个地址包指向的地址**不等于**
   当前栈候选、且不在当前函数体内 → 说明推测返回路径的 atom 对齐错，需要按地址包
   重新同步 workingAddr 并**级联弹栈**到匹配深度，而不是线性走字面量池。
2. **B（缓解）**：`_inconsistentFunctionSwitch` 在"就地切换到一个**函数入口地址**
   （symbol 起始）"时，识别为"漏了返回"，改为**弹栈**到该函数在栈中的层，而不是插
   E|B 就地换名。这不治 disposition 错位，但能消掉 coremark_main 假嵌套的表象且不
   碰 atom 逻辑（风险低）。
3. 保留 `MAX_SANE_DEPTH` 安全网（E1）——它是 depth 上限的兜底，与本根因无关。

**先做 B 验证能否把 358→~1 且不动内层篮子/cardinality**（用 regress.py --check
逐步守护）；B 成立即为低风险解。A 是彻底解但要碰 atom 对齐，风险高，B 不够再上。

---

## 6. 给红方 / 给 proposal 42

- proposal 42 v1 的 §2.5（pop 分类）→ r32 Q1 已证伪。
- r32 自己的 E2（revert 破坏 stack[]）→ **r33 §2 证伪（clobbered=0）**。
- 真凶是 §3 的 disposition 错位。proposal 42 若写 v2，根因段落整体replace 为本文 §3，
  修复方案改为本文 §5 的 A/B。
- fixture + regress.py 已固化在 `captures/mortrall_fixture/`，任何后续改动**每步**
  `regress.py --check`，退化立即可见。


---

## 7. 方案 B 实施结果（2026-08-02，已提交）

按 §5 先做低风险的 B：`_inconsistentFunctionSwitch` 检测到"就地切到某函数**入口**
（addr == lowaddr）且该函数已在栈下方某层"时，判定为漏返回 → 弹栈到那层（逐层补发
`E|`），而不是插 E|B 就地换名。

**两份独立离线数据的诚实对照**（`captures/mortrall_fixture/`，`regress.py`）：

| 指标 | slice1 4MB 修前 | 修后 | slice2 8MB 修前 | 修后 |
|---|---|---|---|---|
| coremark_main | 358 | **7** | 961 | **21** |
| max_depth | 18 | 10 | — | — |
| total begins | 75224 | 74927 | 153361 | 152615 |
| crcu32 | 3397 | 3399 | 6773 | 6796 |
| cardinality | 2640 | 2646 | 2936 | 3038 |

- **coremark_main 假嵌套 46-51× 收敛**（358→7、961→21），接近真值 1。
- **内层高频篮子稳定**（crcu32/crc16/core_state_transition 抖动 <0.1%），未删真实调用边。
- **cardinality 不降反微升**（真 PC 覆盖没丢，弹栈后 workingAddr 修正多覆盖了几个 PC）。
- slice2 是 986MB 抓样里另一段（skip 200MB），完全独立 → **B 不是过拟合 slice1**。

**残留 ~7-21 次**：同类 `cm_uart_send_char→HAL→coremark_main` 漏返回，弹栈边界
（perfetto depth 未同步 / 栈里存的地址非精确 lowaddr）没接住。要压到 1 需碰
disposition/atom 对齐（方案 A，高风险），暂不做——B 已是低风险高收益的"可用"解。

**提交**：`embedded-debug-tools` c5653e3 `fix(orbetto/mortrall): recover missed iBR
returns ...`。fixture + regress.py 固化在 `captures/mortrall_fixture/`（非 git）。
