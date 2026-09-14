# R22c · func_test 真值实测 —— 纠正 r22/r22b 的事实错误 + mortrall A/B 裁决

> **方法**：不靠文档对抗，抓真实 func_test trace，对着 func_test.c 的已知调用树打分。
> **日期**：2026-06-28
> **结论**：r22b 的"最大阻塞=中断处理"基于错误数据；原版 Mortrall 已能解间接/递归/
> 深嵌套；GLM 的 mortrall 改动经实测**更差**，已回退。

---

## 1. ★★★ 纠正 r22/r22b 的核心事实错误：func_test 没有中断

r22b 把"callstack_test 有 78 个活跃中断事件（USART3_IRQHandler、HardwareSerial::
IRQHandler）"列为**最大阻塞问题**，整段中断处理论证建立在此之上。

**这是错的。** 实测证据：

1. **源码**：`func_test.c` 是纯 `while(1){ level_a; indirect_caller; callback_test;
   factorial; deep1; repeat_test; conditional; mixed_test; }` 裸循环，无任何 ISR、无
   USART、无中断使能。
2. **符号表**：`nm proj.axf` —— 0 个 `*_IRQHandler`/`USART`/`HardwareSerial` 的已定义
   符号（只有 startup 的 weak 向量桩，全指向 0x08000276 默认死循环，不会被执行）。
3. **实测 trace**：抓 4MB func_test trace 解码，44 个 distinct PC 里**没有任何中断
   handler 地址**。唯一带 "handler" 字样的是 `cb_handler_a`/`cb_handler_b`——那是
   func_test 的**回调函数**（dispatch_callback 的 BLX 目标），不是中断。

r22/r22b 里的 `USART3_IRQHandler` 等是从**另一个固件的旧 orbetto.perf** 读出来的，
被误当成 func_test 的特征（与"把 func_test 的 trace 当 LVGL 分析"同类张冠李戴）。

→ **r22b 的"最大阻塞=中断处理"作废。** func_test 是确定性裸循环，没有中断要处理，
D-Lite（若做）反而比文档说的少了最大的坑。

---

## 2. ★★★ 原版 Mortrall 已能解间接调用/递归/深嵌套（实测）

抓真实 func_test trace（21M TRACECLK，4MB，0 丢包，1.12% unknown），喂**原版 orbetto/
Mortrall**（仅含已确认正确的 loadelf symtab fallback），对 func_test.c 已知调用树打分
（`decode/verify_functest.py`）：

| 真值项 | 原版 Mortrall 实测 | func_test.c 真值 |
|--------|-------------------|------------------|
| factorial 递归嵌套 | **3** | 4（factorial(4)）|
| deep1→deep6 链深 | **6 ✓** | 6 |
| 间接 op_* 目标 | op_mul, op_sub（2/3）| op_add/sub/mul |
| 回调 cb_* 目标 | cb_handler_a + b（2/2 ✓）| a/b |
| orphan E | **0** | 0 |
| **真值得分** | **13/15** | — |

**结论：原版 orbetto/Mortrall 已经正确重建间接调用（BLX 函数指针）、回调、递归、
6 层深嵌套。** 这直接推翻"orbetto 搞不定函数指针/间接调用"的说法——它本来就行。
之前看起来"搞不定"是两个**输入侧** bug（错误的 want_stream=2 去帧 + loadelf 符号缺口），
都已修复，与 Mortrall 算法无关。

---

## 3. ★★ mortrall A/B 裁决：GLM 改动更差，已回退

同一份 func_test trace，原版 vs GLM 改动版 Mortrall：

| 指标 | 原版 | GLM 版 |
|------|------|--------|
| factorial 递归嵌套 | **3** | 2 |
| deep1→deep6 链深 | **6 ✓** | 3 ✗ |
| 间接 op_* | 2/3 | 2/3 |
| 回调 cb_* | 2/2 | 2/2 |
| B 事件总数 | 17300 | 21314 |
| orphan E | 0 | 0 |
| **真值得分** | **13/15** | **9/15** |

GLM 的改动（间接返回延迟弹栈 + 删除 `_catchInconsistencies` 的 `if(inconsistent)
return` 早返回）让**深层嵌套的栈平衡变差**：deep 链深 6→3、factorial 递归 3→2。B 事件
更多但更浅，说明在不该弹栈处弹了。上游作者保留那个早返回是有道理的。

→ **回退 GLM 的 `mortrall.hpp` 改动**（恢复原版）。loadelf symtab fallback 保留（与
mortrall 无关，已确认正确，命名率 0%→98%）。

---

## 4. 对"要不要重写解析器"（方案 D-Lite）的影响

r22/r22b 否决方案 D、对 D-Lite 设条件，方向判断是对的（OpenCSD ctypes 编造属实）。但
现在实测表明：

- **原版 Mortrall 在 func_test 上已达 13/15**，不是"脱轨"。重写解析器的**前提（现有
  方案不可用）不成立**。
- 唯一真实缺口：factorial 递归差 1 层（3 vs 4）、op_add 偶尔没采到。这是**采样残差/
  region 边界**问题（1.12% unknown），不是解码器算法缺陷——降低 unknown（扫相位）比
  重写解析器更对症。

**建议：不重写解析器。** 保留原版 Mortrall + loadelf 修复。若要把 13/15 提到满分，
方向是降 unknown（func_test 的 TRACECLK 相位微调），不是另写 callstack_tracker。
方案 D-Lite 可作为"若未来 Mortrall 撞到真瓶颈"的备选，但当前无必要动工。

---

## 5. 工具产出
- `decode/verify_functest.py`：对 orbetto .perf 按 func_test.c 已知调用树打分（递归深度/
  链深/间接目标/回调目标/orphan E）。A/B 对比解码器质量的硬标准。
