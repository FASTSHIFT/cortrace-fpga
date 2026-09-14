# r29 响应 — 假调用根因隔离实验（接受红方裁决，执行实验 1+2）

**日期**：2026-07-20
**回应**：`reviews/r29-ETMv4过滤与假调用机理-原文核验红方评审.md`
**立场**：先核验红方自己的支柱引用（Table A-14），确认属实后接受红方全部裁决，
按红方指定顺序执行实验 1（ELF↔flash 同一性）+ 实验 2（packet-level 根因隔离）。

---

## 0. 先核验红方的支柱证据（不把红方断言当事实）

红方裁决建立在"Table A-14 证明 RS=0 时返回发 Address element"上。核验
`refs/ihi0064h.txt` Appendix A.6，Table A-14（RS **disabled**，line 44057）：

```
0x2014  BX LR    atom_element(E)              ← 返回本身只发 atom
0x1004  MOV      address_element (0x1004)     ← 返回目标由 trace unit 发 Address
                 注释原文: "the last instruction executed was a taken indirect branch
                 instruction, so the trace unit generates an Address element to
                 indicate the target of that branch."
```

**红方引用属实且精确**：RS=0 时返回目标确实由 trace unit 发 Address element = 可靠锚点。
**接受红方裁决**：我的 C6/C7 把 RS=1 才有的"返回不发地址、返回栈补错"机制误套到 RS=0 配置
（本机 TRCCONFIGR=0x9, RS=0），且 C7 把手册 Appendix A 已明确回答的问题标成"待验证【推断】"
是可查未查的搪塞。**C6/C7 作为当前假调用成因链的部分：撤回。**

---

## 实验 1（最高优先级）— ELF↔flash 逐字节同一性校验

红方 Q4 最大盲区："假调用可能来自 ELF≠固件"，从未被验证。

**做法**：确定编译 150M+cache+BB-OFF 观察配置的 ELF（`/tmp/exp1_150m.elf`），objcopy 出 bin，
openocd `dump_image` 从 flash 读回同长度 44524 B，`cmp -l` 逐字节比对。

**结果**：
```
cmp -l exp1_150m.bin flash_readback.bin  →  仅 12 字节不同
  offset 665-672 (8B): ELF=0x00 vs flash=0xFF  (.isr_vector 段尾间隙)
  offset 42205-42208 (4B): ELF=0x00 vs flash=0xFF  (bank 写对齐尾)
所有代码/数据字节 100% 一致。
```

**判定：ELF≠固件假设排除。** 交给 OpenCSD 的 ELF 与 flash 镜像代码逐字节一致，差异全是
擦除值填充的段间隙。红方 Q4 的最大替代解释（工程 bug）**证伪**。可以继续追 ETM 机理。

---

## 实验 2（根因隔离）— 假调用目标是"盲推 PC"还是"流中真 Address"

红方重定义的问题：假调用 `HAL_UART_Init` 的目标地址，是解码器 atom 盲推到达（→C4 纯盲推），
还是流里有真 `I_ADDR_*` 包指向它（→回采集完整性，推翻 R1）。

**用确定 ELF 重解 150M BB-OFF+cache 流**，假调用复现：`core_list_init` 44 PC、
`HAL_UART_Init` 25 PC。开 `trc_pkt_lister -decode` packet-level 日志（6838 行）逐包核查：

### 证据 1：流里无任何真 Address 包落在 HAL_UART_Init 本体
```
grep I_ADDR_S/L 落在 0x791c-0x79f4 (HAL_UART_Init 体)  →  0 个
全部 54 个真 Address 包（I_ADDR_S_IS/I_ADDR_L）+ 482 个 I_ADDR_MATCH，
0x79xx 附近的真地址其实都指向 0x7BD8/0x7C18/0x7C52（cmp_complex）、0x7F00（core_bench_list）
—— 都是 CoreMark 真实执行的函数，非 HAL。
```
**⇒ 假调用目标不是流中的真 Address 包。** 排除"采集把地址采成 0x79xx"，与 R1 一致。

### 证据 2：假调用是条件分支 atom 盲推越界（决定性）
packet 日志 Idx:2452 关键序列：
```
exec range=0x8007ff2:[0x8007ffa] ... E BR ...     ← core_list_init 内条件分支
exec range=0x80079c0:[0x80079d8] ... N BR <cond>  ← 解码器"执行"跳进 HAL_UART_Init 区(0x79c0)
```
- `0x80079c0` = `b.n 0x800794e`（HAL_UART_Init+0x32 内部分支）。
- **core_list_init（0x7f54-0x8010c）反汇编：无任何 BL/BLX 调用指令**，全是内部条件分支
  （bls/beq/bne），如 `0x7ff2: beq.w 0x80080fe`。
- 即 `0x79c0` **不是任何真实调用/分支的静态目标**，是解码器盲推链上的错误落点。

**机制（现在精确）**：BB-OFF 下 core_list_init 的条件分支只发 atom(E/N)、不发地址（C4）。
cache 全速使地址锚点稀疏（3200 atom / 13 锚点）。盲推链上**某个条件分支的 atom 方向与真实
执行错位**（atom 计数/方向漂移），PC 被带偏，顺 ELF 反汇编走进物理地址 0x79c0（属 HAL_UART_Init
符号区），`func_of` 最近符号归属把它算成 HAL_UART_Init → 假调用。持续到下一个真 Address 锚点
（0x7F00 的 I_ADDR_MATCH 反复出现 = 锚点在 core_bench_list，不在 core_list_init）才纠回。

---

## 裁决（回应红方 r29）

| 红方质疑 | 本轮实测裁决 |
|------|------|
| C6/C7 返回栈补错在 RS=0 不成立 | **接受**：核验 Table A-14，RS=0 返回发 Address；撤回 C6/C7 作为成因 |
| C7【推断】搪塞（手册已答） | **接受**：Appendix A 已答，不该标"待验证" |
| Q4：ELF≠固件未排除 | **实验 1 排除**：ELF↔flash 代码字节 100% 一致（仅 12B 段隙填充）|
| Q4：解码器 call 重建 bug / deframe 错位 | **实验 2 定位**：deframe 正确（真 Address 全落合法函数），假调用是 `func_of` 对**盲推错误落点**的最近符号归属，非 deframe 错位 |
| 假调用主因 | **纯 C4 条件/直接分支 atom 盲推越界**（返回栈不参与），坐实红方 Q4 分叉 (a) |

**总结论修正**：假调用 = **BB-OFF 下条件/直接分支只发 atom、cache 拉稀锚点 → atom 方向盲推
漂移 → PC 越界进物理相邻符号区**。R1 的"非采集"结论**不被推翻**（实验 2 证真 Address 包全
落合法函数、无脏地址）；但 C6/C7 的"返回栈"叙事**撤回**（RS=0 不适用，且根本没参与）。
这仍是 ETM 压缩模型（atom 盲推）× cache 稀疏锚点的解码局限，但机制是**atom 方向漂移**，
不是"返回不锚定"。

**方法论教训（记账）**：C6/C7 又犯了"把部分条件（RS=1）成立写成普适"+"用【推断】标签搪塞
可查未查的问题"——红方 r29 点名的两条老毛病属实。改正方式已内化：**引用手册结论前先查
该手册的示例/附录章节（Appendix A 就有 RS on/off 对照表）**，不把知识空白包装成客观不确定性。

---

## 下一步

假调用根因锁定为 atom 盲推漂移（非采集、非 ELF、非返回栈）。这与 R2 裁决一致——BB-OFF 在
cache 全速下的调用图不可靠是**解码固有局限**，缩盲推跨度的片上手段又硬件不可用（候选 ab）。
故方向仍是 R2 定的**采样式统计 / 解码器侧盲推约束**二选一，但需注意：既然根因是 atom 方向
漂移（而非单纯锚点间距），"解码器侧函数边界不跨界"约束能否奏效存疑（漂移发生在边界内的条件
分支上）——**倾向直接走采样式统计**（多次抓样统计热点函数频次，容忍单次盲推漂移），更诚实
可靠。待与用户确认方向。
