# r32-response — 对红方评审 r32 的蓝方回应

**日期**：2026-07-26
**对象**：`reviews/r32-mortrall栈重构-红方评审.md`
**态度**：**红方对，proposal 42 需要重写**。P0 三个实验都跑了，Q1 证伪、E1 部分成立、Q5 排除。真凶方向变了，本文汇总实测结果并给下一步计划。

---

## 1. 红方逐条打击的接受度

| 议题 | 红方判定 | 蓝方回应 | 状态 |
|---|---|---|---|
| Q1 pop 分类失败假设 | 🟥未证且大概率证伪 | **实验证伪，红方 100% 对** | ⛔ proposal 42 §2.5 撤回 |
| Q2 拆栈时序/单调性 | 🟥未算过账 | 接受 | 若走重构必须补 fpga_ns 分配算法 |
| Q3 top-match 缺 IS bit | 🟨窄影响 | 接受，若走返回栈优化实现必须加 | — |
| Q4 路径选择性循环论证 | 🟥 | 接受，且 E1 实验证明路径无关（安全网触发才有"选择性"） | 撤回 §2.6 论证 |
| Q5 opencsd 漏发 iBR | 🟨可低成本排除 | **实测 iBR=13492, BL=7897，充足**，排除 | ✅ opencsd 不背锅 |
| Q6 replay 单测可行性 | 🟨蓝方低估 | 接受，改双抓样对照 | ✅ 撤回 §5.4 |
| Q7 时序估算 | 🟥低估 2× | 接受 | — |
| Q8 备选方案对比缺失 | 🟥违反方法论 | 接受，必须先跑 tag 位 / shadow 栈 | ⛔ proposal 42 §3 大重构方案暂缓 |
| E1 MAX_SANE_DEPTH=16 | 🟥首要嫌疑 | **实验部分成立** | 见 §3 |
| E2 resentStackDel revert | 🟨完全没提 | 接受，值得单独验证 | 待做 |

---

## 2. Q1 实验：pop 分类 —— **证伪**

**方法**：在 `_pumpAction` 里 pc == 0x0800a2be (cm_uart_puts pop) 或 pc == 0x0800a2f2
(cm_uart_send_char pop) 时 dump ic 值。

**结果**：
```
[R32-Q1] pc=0x0800a2f2 ic=0x00000001 JUMP=1 CALL=0 IMM=0 4BYTE=0 SYNC=0 asm=' 800a2f2: bd10 pop {r4, pc}'
[R32-Q1] pc=0x0800a2be ic=0x00000001 JUMP=1 CALL=0 IMM=0 4BYTE=0 SYNC=0 asm=' 800a2be: bd70 pop {r4, r5, r6, pc}'
```
**每次都是 `ic=0x01, JUMP=1, CALL=0, IMM=0`** — pop 分类完美，全部走 iBR/return 分支。

**结论**：proposal 42 §2.5 猜测（"pop 分类失败导致顺序推进"）**证伪**。§2.5 那段推理
和整个 §3 拆栈方案的具体触发点都需要重推。

---

## 3. E1 实验：MAX_SANE_DEPTH=16 安全网 —— **部分成立**

**方法**：把 `_addRetToStack` 里 `if (stackDepth >= 16) flush` 分支 `#if 0` 掉，跑同一
500KB 抓样。

**结果**：
| 指标 | 有安全网 | 关安全网 |
|---|---|---|
| coremark_main 出现次数 | 257 | **44**（5.8× 骤降） |
| 最大栈深度 | 16.5 | **29.5** |
| PC bitmap cardinality | 3483 | 3580 |

**解读**：
1. **安全网确实是"257 次"这个数字的主要贡献者** —— 每次 flush 平栈后重新 push，就产
   生一批 B|coremark_main 事件。关掉后不 flush，事件少了。
2. **但栈依然自由增长到 29.5** —— 说明**真实的漏 pop 存在**，安全网只是把症状包装成
   "深度 16.5 反复触底"。真正的 bug 是 push > pop。
3. E1 是"症状放大器"不是"根因"。

**下一步**：不能简单把安全网删了 —— 关掉后 CoreMark 上下文栈可能爆到 MAX_CALL_STACK。
必须先修 push/pop 不平衡。

---

## 4. Q5 实验：iBR vs BL 事件数比对 —— **opencsd 排除**

500KB 段 opencsd lister 统计：
```
b+link (BL/BLX)     : 7897
iBR total           : 13492
  iBR V7:impl ret   : 8671
  iBR (其他)         : 4821
```

**分析**：
- BL 数 7897 —— 应对应 7897 次 push。
- impl ret 8671 —— 应对应 8671 次 pop（包含 return + EXC_RETURN）。
- 4821 个"其他 iBR" —— tail call / function ptr / 跳转表 / 异常入口，**不 pop 也不 push**。

**理论上 pop - push ≈ 8671 - 7897 = 774**（异常返回带来的额外 pop）。

**但 Mortrall 内部打点**（上一轮诊断）显示 `pop=16909, push=10546, delta=-6407` —— pop
比 push 多 6407 次。这**远超**从 opencsd 侧算出的 774。说明 **Mortrall 的 orbuculum 解
码器对同一份流的 iBR/BL 计数与 opencsd 不同**，或 Mortrall 把某些 iBR 处理成多次 pop。

**Q5 结论**：opencsd 层数据完整（iBR 13492 充足），**根因在 Mortrall/orbuculum ETMv4
解码器 或 Mortrall 的 iBR 处理逻辑**，不是 opencsd 漏发。

---

## 5. 修正后的根因假设

根据 P0 三个实验，重新推导：

**假设 H'**：Mortrall 的 pop 比 push 多，且**多 pop 不是靠 iBR 触发**（否则应受 opencsd
iBR 事件数上限约束）。**过度 pop 来自 `_inconsistentFunctionSwitch` 塌栈** 或
`_handleExceptionExitETM35` 的 while 清栈 或 `resentStackDel` 的 revert 撤销后的重
pop。

具体候选路径：
1. `_inconsistentFunctionSwitch`（line 1345-1360）—— 遇到"新地址在栈里已有函数同名"就
   塌到该层。上一轮统计里 `_inconsistentFunctionSwitch` 只触发 29 次 / pop 44 次，占
   pop 总数 0.3%，**不是主要来源**。
2. `_handleExceptionExitETM35` 的 while 清栈（line 1090-1103）—— 3 次 SysTick × 平均
   每次栈深，量级不大。**关 SysTick 后 pop 依然 > push**，也不是主因。
3. **`resentStackDel` 推测性弹栈 + 撤销**（line 1281-1298 + iBR 分支 line 667-679）—
   iBR 时先 pop，若下一步 address 不一致 → stackDepth++ 撤销 —— **撤销后 stackDepth
   涨回，但被 pop 出的 stack[oldDepth] 内容已被后续 `_addTopToStack` 覆盖**！

**H' 具体化**：每次 iBR 触发 → pop → 若 `_revertStackDel` 判定不一致 → 撤销（depth++），
但 `stack[depth]` 已经变了。这样"看着 pop 了又还回来"但栈内容错乱。红方 E2 的判断是
正确的方向。

**接下来必做**：把 `_revertStackDel` 的触发计数、每次触发时的 (workingAddr,
stack[depth]) 差异打出来，看**revert 是否是主要泄漏源**。

---

## 6. 修正后的行动计划（严格按红方要求）

### P0（本周内）

- [x] **§5.1 pop 分类实验**（红方 Q1）—— 完成，证伪
- [x] **关 MAX_SANE_DEPTH 重跑**（红方 E1）—— 完成，部分成立
- [x] **iBR vs BL 事件数比对**（红方 Q5）—— 完成，opencsd 排除
- [ ] **`_revertStackDel` 触发计数 + 一致性 dump**（本响应新增 §5，红方 E2 延伸）—— 未做

### P1（P0 完成后）

- [x] **`_revertStackDel` 触发计数**（红方 E2 延伸）—— **完成，是主漏 pop 来源**：
  - iBR pop hits **16509**, revert hits **5355** (**32.44%**), max stack depth **16**
  - 每 3 次 iBR pop 就有 1 次被 revert，且 revert 只增 depth 不恢复 stack[] 内容 →
    stack[oldDepth] 已被 `_addTopToStack` 覆盖成中间 PC。**E2 完全证实**。
- [x] **备选方案 B：shadow 栈**（<20 行）实验 —— **失败**：
  - 加 shadow 保存 push 时的返回地址，iBR 时依然读 stack[depth-1]（那时 stack 未被覆盖，
    等价于原逻辑），revert 时从 shadow[depth] 恢复 stack[depth]。
  - 单独让 iBR 读 shadow[depth-1]：与 stack[depth-1] 值相同，coremark_main 不变
    （依然 257 次）—— **iBR 那点原代码没错**。
  - 加 revert 恢复 stack[]：orbetto **陷入死循环 / 极慢**，60 秒跑不完（baseline 20 秒），
    stdout 不再输出 finalize 阶段。
  - **红方 Q2 时序警告应验**：revert 恢复 stack[] 后立即影响 protobuf 事件生成
    （`_appendTOProtoBuffer` 的 cycle-count buffer / perfettoStackDepth stepping），
    引发未预期的循环或性能崩溃。
  - 已撤回代码到 baseline，仅保留诊断计数注释。
- [ ] **备选方案 A：tag 位**（<30 行）—— 待做。逻辑上比 shadow 更清晰：
  `_addTopToStack` 只写 CURSOR 标签，iBR 弹时 while 跳过 CURSOR 找 RET_ADDR。理论上
  避开 revert 恢复 stack[] 引入的时序耦合，但也可能同样触发 protobuf 层问题。

### P2（P1 全部达不到目标才做）

- [ ] proposal 42 §3 大重构 —— **必须**补 §Q2 的 fpga_ns 分配算法、§Q3 的 IS bit 匹配、
  §E2 的 revert 语义迁移，还要**同时**修 protobuf buffer 的 perfettoStackDepth stepping
  逻辑。工时 2-3 天不止，接近 1 周。红方 Q7 低估警告完全成立。

---

## 7. proposal 42 该怎么处理

**结论**：proposal 42 **撤回，改写为 v2**。v2 必须包含：
1. §2.5 删除，改成"根因在 iBR 弹栈后的 revert 撤销 + stack[] 覆盖"（待 P0 §5 实验坐实）
2. §3 大重构方案降级为"备选方案 P2"
3. §3 前面新增"备选 A tag 位 / 备选 B shadow 栈"作为 P1 首选
4. §5 实验清单里 pop 分类实验放前置，且必须证伪 §2.5 才允许推进
5. §8 时序估算翻倍
6. **诚实标注**："proposal 42 v1 的 §2.5 猜测已被 r32 Q1 证伪，v2 根因假设仍待
   `_revertStackDel` 计数实验确认"

---

## 8. 一句话给红方

**你 Q1、E1、E2、Q2、Q5 全部命中，proposal 42 v1 撤回。P0 三个实验都跑了：pop
分类完美（Q1 证伪 §2.5）、安全网确实是 "257" 的放大器（E1 成立）、iBR 事件数
13492 充足（Q5 排除 opencsd）。你 E2 的"resentStackDel revert 是主漏 pop 来源"
也实测证实：iBR pop 16509 次里 5355 次 revert (32.44%)。你 Q4 也对，"1-2 层能
覆盖修复"这句证不成立。**

**shadow 栈小 fix（备选 B）实验也做了 —— **失败**。让 revert 从 shadow 恢复
stack[] 立刻触发 orbetto 死循环 / 60 秒跑不完（baseline 20 秒），你 Q2 的时序
警告完全应验。**修 revert 需要同时协调 protobuf buffer 的 perfettoStackDepth
stepping，不是纯栈层问题**。**

**结论：**
- 现有 baseline（257 次 coremark_main / 深度 16.5）**是最不坏的当前状态**，一动
  就更糟。
- **tag 位（备选 A）**还没试，可能同样撞 protobuf 时序问题，也可能因为语义更清
  晰而工作 —— 计划试一次，但不指望。
- **最终方案很可能是 proposal 42 §3 大重构**（同时改栈 + protobuf buffer），
  按你说的 1 周工时预留。

**当前实用建议**：Perfetto UI 打开时视觉忽略最外层 coremark_main 的深嵌套（真正
调用图内层是准的，verify_calls 678/678 全对），或用 `verify_calls.py` 做诚实
判据。这是"半成品可用"的状态，比强行修引入回归好。**
