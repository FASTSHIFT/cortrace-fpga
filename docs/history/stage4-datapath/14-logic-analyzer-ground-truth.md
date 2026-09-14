# Stage-4 · ★★★ 逻辑分析仪 ground-truth 对拍:整条解码链路实测验证通过

> 用户用 50MSa/s 逻辑分析仪(DSLogic U2Basic / DSView)直接抓 STM32 的 5 根 trace 引脚,
> 跑已知固件 `while(1){__NOP();}`(`proj_while_nop.axf`),把物理引脚的真值拿来对拍。
> **这是整个 Stage-4 第一次有"物理 ground truth",一锤定音地证明了解码链路正确。**

---

## 0. 结论(决定性)

**逻辑分析仪抓的真实引脚 bit → 解码 → I-sync PC = `0x08000ff0` = ELF 里 `while(1){__NOP();}` 的确切地址。** 整条链路(nibble 映射 / DDR 边沿序 / ETM 字节组装 / I-sync 提取)**全部实测正确**,不再是推断。

```
8000fe4 <main>:
 8000ff0: bf00   nop            ← 我们解出的 I-sync PC(29 次)
 8000ff2: e7fd   b.n 8000ff0    ← while(1) 回跳
```

---

## 1. 实验设置

| 项 | 值 |
|----|----|
| 逻辑分析仪 | DSLogic U2Basic,50 MSa/s,5 通道 |
| 降频 | HCLK /64 ≈ 2.6 MHz(`downclock.cfg DIV=64`) |
| TRACECLK 实测 | **1313 kHz**(.dsl 解析,与 /64 预测吻合) |
| 接线 | D0=PE2(CLK) D1=PE3(TD0) D2=PE4(TD1) D3=PE5(TD2) D4=PE6(TD3) + 共地 |
| 固件 | `while(1){__NOP();}`,ELF `proj_while_nop.axf` |
| 采集 | 50M 样本,RLE off,无外部时钟 |

## 2. 解析(`dsl_parse.py`)

`.dsl` 是 zip:`header`(ini)+ 每通道 bit-packed 块 `L-<ch>/<blk>`(每字节 8 样本,LSB 在前)。
1. 解 5 通道 → 找 TRACECLK 边沿(52505 上升 + 52504 下降)。
2. **DDR**:每个边沿采一次 D0-D3 = 一个 nibble。
3. 两种边沿序拼字节对比:

| 边沿序 | A-sync 数 | 判定 |
|--------|----------|------|
| **rise=低 nibble, fall=高 nibble** | **31** | ✅ 正确 |
| rise=高, fall=低 | 0 | ✗ |

→ **实测确定:TRACECLK 上升沿 = trace_a(低 nibble),下降沿 = trace_b(高 nibble)**。这把我们之前反复纠结的 nibble/边沿映射**一次定死**。

## 3. 解码结果(对拍)

`rise=low` 字节流(52504 字节)喂 `etm35lib`:
- **A-sync = 31,I-sync(严格,含 r14 加固)= 42**
- I-sync 绝对 PC 直方图:

| PC | 次数 | addr2line(proj_while_nop.axf) |
|----|------|------|
| **0x08000ff0** | **29** | **`while(1){__NOP()}` 的 nop(main+0xc)** ✅ |
| 0x08000ef2 | 12 | 中断/handler 上下文(0x8000eec 区段) |
| 0x08000fd0 | 1 | main 之前(Delay_Init 附近) |

字节流主峰是 **0x88(21635 次)= ETM Format-1 P-header"执行了 2 条指令"** —— 完全符合 `nop; b` 这种两指令紧循环的预期。

## 4. 这验证了什么(把推断变实测)

| 之前状态 | 现在 |
|----------|------|
| nibble 高低位映射(反复猜) | ✅ 实测:rise=低 nibble |
| DDR 哪个沿是高 nibble | ✅ 实测:上升沿=trace_a |
| 是裸 ETM 还是 formatter | ✅ 裸 ETM(A-sync/I-sync/P-header 结构,无 TPIU 帧) |
| I-sync PC 解码正确性 | ✅ **= 真实 while(1) 地址,ground truth 对上** |
| etm35lib 解码逻辑 | ✅ 物理真值 + golden 单测双重确认 |
| r14 BUG-1 锚点可信度 | ✅ 加固后的严格锚定,42 个全部落在真实代码地址 |

## 5. 意义

- **采样链 + traceIF + 解码器整条 = 实测正确**。之前所有"指示性/推断"的限定词,在 ground-truth 这条上可以去掉了。
- 真实 LVGL 抓取里解出的那些函数(lv_timer_handler 等)**因此也是可信的**——同一条已被 ground truth 验证的链路。
- 剩余的"连续流"问题是**覆盖率/采样窗口**问题(锚点间靠 P-header/branch 续解、会脱轨),不是正确性问题。正确性这一关,过了。

> 一句话:**逻辑分析仪给我装上了眼睛。抓 `while(1){__NOP();}`,解出的 I-sync PC = `0x08000ff0` = 那个 NOP 的确切地址。整条 trace 解码链路第一次有了物理 ground truth,实测验证通过——nibble/边沿映射、裸 ETM 判定、I-sync 解码全部坐实。**


---

## 6. proj_add 对拍 + 台阶① 连续逐指令重建(实测)

> 把固件从 `while(1){__NOP();}` 换成有真实控制流的 `proj_add`,验证解码链路不仅能
> 锚定单点 PC,还能**逐条指令重建执行路径**。

### 6.1 固件 ground truth(反汇编)

```c
int add(int a,int b){ return a+b; }                 // _Z3addii  @0x08000f8c
int loop_sum(int n){ int s=0;
    for(int i=0;i<n;i++) s+=add(i,i); return s; }   // _Z8loop_sumi @0x08000fa4
while(1){ volatile int x = loop_sum(5); __NOP(); }
```

```
08000f8c <_Z3addii>:        08000fa4 <_Z8loop_sumi>:
 f8c: mov  r2, r0            ...
 f8e: adds r0, r2, r1         fb2: bl   8000f8c <add>   ← 调 add(每次循环1次)
 f90: bx   lr                 fbc: blt.n 8000fae        ← for 回跳(取5次/不取1次)
```

### 6.2 抓取 + 解码(`DSLogic ...162903.dsl`,HCLK /64,TRACECLK 1313 kHz)

CLK 检查完美(0% 数据未对齐)。`etm_decode_cli` 锚点解析:

| PC | addr2line | 判定 |
|----|-----------|------|
| 0x08000f8c | `_Z3addii` (main.cpp:22) | ✅ add 函数 |
| 0x08000fb2 | `_Z8loop_sumi` (main.cpp:23) | ✅ loop_sum 里 `bl add` |
| 0x08000fbc | `_Z8loop_sumi` | ✅ loop_sum 的 `blt` 回跳 |
| 0x08000e90 / 0x08000c90 | TIM IRQ / SystemInit | 中断/启动上下文(预期内) |

锚点全部命中预期函数。

### 6.3 台阶①:连续逐指令重建(`etm_reconstruct.py`,纯软件)

新增 `etm35lib.expand_pheader`(P-header→有序 atom 列表,IHI0014Q Table 7-2)+
`decode_branch_thumb`(压缩分支地址,Fig 7-4)+ `etm_reconstruct.py`(对着 ELF 反汇编
走 PC:**直接分支目标从镜像推算,间接分支吃 Branch Address 包**,IHI0014Q §4.5.2/§4.10.3,
与 orbuculum `traceDecoder_etm35.c` 模型一致)。

实测重建出 add() 的**精确三指令序列**:
```
0x08000f8c  mov  r2, r0
0x08000f8e  adds r0, r2, r1
0x08000f90  bx   lr            ← 间接返回,吃下一个 branch 包
```
以及 loop_sum 的 `bl add` / `blt` 回边。

**诚实边界**:连续区段最长约 7 条指令就因 4-bit 口的子字节失步而脱轨(中位数 2 条),
随后由下一个 I-sync 重新锚定。**解码正确性 = 实测通过;连续性 = 受当前有损采样限制**。
这正是台阶①(纯软件解码,已达成)与台阶②(满速无损采样,物理命门)的分界:
逐指令重建逻辑本身正确,要拿到长连续路径需要采样侧不丢数据。

### 6.4 回归固化

- 提交了两个稳定 fixture:`captures/while_nop_ground_truth.bin`(0x08000ff0)、
  `captures/proj_add_ground_truth.bin`(含 add/bl-add/blt 三锚点的 16KB 窗口)。
- `test_etm_reconstruct.py` 新增 20 用例(指令分类、Thumb 分支解码、间接分支吃包、
  I-sync 中途重锚、proj_add 三指令序列集成)。全套 **120 用例通过**,etm35lib 覆盖率 97%。
- ground-truth 测试不再依赖易失的 `/tmp/dsl_bytes_0.bin`,改读committed fixture。


---

## 7. dsl_parse 固化 + TPIU 同步填充剥离(branch-broadcast 关闭后)

### 7.1 关掉 branch broadcast 重抓(172817.dsl)

按"路 2"把 `ETMCR` bit8(branch broadcast)清掉(`0xd80/0x980 → 0xc80/0x880`,
已上板读回 `0x880`),让流匹配镜像驱动重建器的模型(直接跳转目标查 ELF、间接跳转吃包)。

### 7.2 两个真问题被揪出来

**问题 A — DDR edge-parity 偏移(解析器 bug)**:172817 这次采集从 DDR 半拍**中间**开始,
整体错位一个时钟沿,旧 `dsl_parse`(硬编码 `rise=low`、从第一个沿开始配对)把每个字节都搅坏,
直接表现为"满屏 `0xFFFFFF7F`、0 个锚点"。

→ **修复**:`dsl_parse.py` 重写为**自动搜索对齐**——试全部 4 种(parity 0/1 × nibble 序
low/high),用"flash 区间合法 I-sync 锚点数"打分挑赢家;中眼采样(沿后半个半周期);
任何对齐都解不出锚点时大声告警。验证:162903 自动选 parity=0(332 锚点,和旧硬编码一致),
172817 自动选 parity=1(92 锚点,旧解析器是 0)。

**问题 B — TPIU 同步填充(真实存在,非 artifact)**:对齐修正后,流里**确实**有
33902 个 `0xFFFFFF7F`(满同步)+ 大量 `FF 7F`(半同步)。这是 TPIU formatter 在 ETM 空闲时
往链路里填的同步字。branch broadcast 关掉后数据稀疏,填充占比更高(652788 有效字节 vs 填充
659826 字节,几乎一半)。

→ 关键判断(实测):这颗单源 STM32 ETM **不是 stream-ID 分帧**(按 stream 去帧会散成几十个
假流、0 锚点),而是**裸 ETM + 同步填充**。所以正确前端是**剥离填充**,不是跑 16 字节去帧器。
新增 `etm35lib.strip_tpiu_sync`(去 `0xFFFFFF7F` 满同步 + `FF 7F` 半同步)+ `has_tpiu_sync`,
并集成进 `dsl_parse.py`(检测到填充自动剥离并报告)。同时把 orbuculum `_getPacket` 的完整
16 字节去帧器也忠实移植为 `tpiu_deframe`(供真·多源分帧场景,golden 测试覆盖)。

### 7.3 效果

剥离填充后重建,proj_add 最长连续路径从 **4 条指令提升到 33 条**(能看到 loop_sum 的
`add r5; adds r3; cmp; blt` 完整循环体)。**这坐实了:低速采样物理无损,卡点在解码前端
没处理 TPIU 填充。**

仍有少量错步(如 `bl` 后跨函数跳错),是 from-scratch 重建器在调用/返回配对上的模型不全,
属台阶①待完善项(可对齐 orbuculum 状态机解决),不影响"采样无损"这个结论。

### 7.4 回归

新增 13 个测试(strip 5 种情形 + has_tpiu_sync + 透过填充恢复 I-sync + tpiu_deframe golden),
**全套 129 用例通过**。`dsl_parse.py` 现在是健壮可复用脚本:喂任意 `.dsl`,自动定对齐、
自动剥 TPIU 填充、报告锚点、写 `/tmp/dsl_bytes_0.bin`。


---

## 8. 复用 orbuculum 解码核心(orbetm)+ 对照验证

### 8.1 动机

我之前自研的 `etm_reconstruct.py` 在 call/return 配对上会错步。与其继续追 orbuculum 的状态机,
不如**直接复用它**。做了 `orbetm.c`:link orbuculum 的 `traceDecoder*` + `loadelf.c`(capstone
反汇编),忠实搬 orbmortem 的 `_traceCB` 指令流重建循环,去掉 ncurses TUI,逐条打印执行指令。

### 8.2 编译坑(回答"为什么官方能编我编不出来")

裸 `cc` 一开始失败,两个根因都是**没复刻 orbuculum 的 meson 环境**:
1. **`dwarf.h` 找不到**:orbuculum 把 **libdwarf 作为 meson subproject 自带**(`subprojects/
   libdwarf-0.7.0/`),不用系统 dwarf.h。本机只装了运行时 `libdw1`、没装 `-dev` 头。
   → 指向 vendored 头 + 已编好的 `build/subprojects/.../libdwarf.so`。
2. **`C_VERB_* undeclared`**:meson 全局 `-include uicolours_default.h`(定义颜色宏)。
   → 手动加 `-include`。
3. 还漏链 `readsource.c`(loadelf 依赖)。

修好后见 `build_orbetm.sh`。官方一直能编,编不出来的是我绕过 meson 的手写命令。
orbuculum 无 release tag(滚动 main,内部版本 2.2.0);当前是 debug 构建,需要时
`meson setup build_rel --buildtype=release` 即可出 -O2 版(已验证可编)。

### 8.3 对照验证(决定性):runaway 是数据固有,非解码器 bug

把 172817 剥填充流喂 orbuculum **自己的**解码器统计地址事件落点:

| 指标 | 值 |
|------|----|
| 解出的 ADDR 事件总数 | 343366 |
| 落在 flash 代码区的 | 91986(**26.8%**) |
| branch-broadcast ON(162903)同样测 | 62728/214451(**29.3%**) |

**两份流都只有 ~27-29% 地址落在代码区,73% 是乱解。** 这证明:**orbuculum 久经考验的解码器
在我们这份流上和我自研的表现一样会跑飞**——根因是 branch-broadcast-off 的稀疏锚点流,间接
返回(`pop pc`/`bx lr`)在调用栈不完整时拿不到目标地址,PC 失控走进向量表。**这是数据特性
的固有限制,不是哪个解码器实现的 bug。**

### 8.4 orbetm 的处置:窗口门控,只信能落在代码区的

给 orbetm 加了代码窗口门控(跳过 0x08000200 以下的向量表;PC 离开 flash 代码区就停止当前
run、等下一个 I-sync/branch 重新锚定)。门控后从 172817 流稳定解出真实函数:
`TIM8_UP_TIM13_IRQHandler`、`TIM_GetITStatus`、`USART_ClearITPendingBit`、`USART3_IRQHandler`、
`HardwareSerial::IRQHandler` 等——与锚点法解出的函数集一致,且现在是**逐指令流**(带反汇编)。

### 8.5 结论

- **复用达成**:解码核心从自研切到 orbuculum(`orbetm` link liborb 解码器 + capstone)。
  `etm35lib` 降级为锚点快速校验 + 测试夹具,不再承担完整指令重建。
- **诚实边界没变**:稀疏流下的连续逐指令 100% 还原,是 orbuculum 也做不到的——卡在数据,
  不在解码器。要真正连续,得回到台阶②(更密的同步/不丢的满速采样),不是换解码器能解决。
- `dsl_parse.py`(.dsl → 剥填充裸 ETM)依然是我们独有、不可替代的前端。


---

## 9. 连续性的两个杠杆:分支广播(有效)+ 同步周期(硬件锁死)

台阶②的目标是让锚点更密、解码器不跑飞。能动的旋钮只有两个,实测验证如下。

### 9.1 杠杆 A:branch broadcast(ETMCR bit8)—— 有效,7×

把两份已有 capture 喂 orbetm(orbuculum 解码核心)比"落在代码区的指令占比":

| capture | ETMCR | executed(代码区) | dropped(跑飞) | 代码区占比 |
|---------|-------|------------------|---------------|-----------|
| 172817 | 0x880(bcast **OFF**) | 39099 | 1456967 | **2.6%** |
| 162903 | 0x980(bcast **ON**)  | 70163 | 303836  | **18.8%** |

**branch broadcast ON 把可用指令占比从 2.6% 提到 18.8%(7×)。** 原理:bit8=1 让**每个直接跳转
也吐地址包**,于是解码器在硬件固定的 1024 字节 I-sync 周期之间,靠这些地址包不断重新锚定;
关掉它,一遇到拿不到目标的间接返回(`pop pc`/`bx lr`)PC 就失控冲进向量表。

→ **结论性修正**:之前为了迁就我手写的 image-walk 解码器把 bit8 关了,那是错的方向——它饿死了
真正的流式解码器。已把 `etm_enable.cfg` 改回 **0xd80/0x980(bcast ON)**。

### 9.2 杠杆 B:ETM 同步周期(ETMSYNCFR)—— 硬件锁死,动不了

ETMSYNCFR(0xE00411E0)本想缩短到 256B/64B 让 I-sync 更密。实测:**只读,固定 0x400=1024 字节**
(写 0x100、0x40 都读回 0x400)。这颗 M4 的 ETM 同步周期硬件钉死,没法调。

### 9.3 当前可行的最优 + 下一步

- 最优配置 = **branch broadcast ON + 降频保证采样无损**(已设好:ETMCR=0x980,HCLK /64)。
- 用这个配置重抓一份,预期连续段比 172817 长得多(代码区占比 ~19% vs 2.6%)。
- 残余的跑飞来自间接返回 + 1024B 周期的固有空窗,这是 ETM-M4 的物理上限,不是软件能再压的。
  要再进一步只能上**满速 + 不丢传输的连续流**(台阶②/③),或换有更多比较器/可调同步的更高端
  trace 单元(商业 J-Trace 级)。


---

## 10. broadcast ON 重抓(194702)实测:锚点可信,连续流仍受 ETM-M4 物理上限

### 10.1 抓取与配置

`DSLogic ...194702.dsl`,ETMCR=0x980(branch broadcast ON)+ HCLK /64。CLK 完美(0% 未对齐,
4.69M 数据跳变,比 broadcast off 的 2.9M 更密,符合预期)。`dsl_parse` 自动选 parity=1、剥
TPIU 填充,得 **334 个 flash I-sync 锚点**。

### 10.2 可信的部分:I-sync 锚点 = ground truth

334 个锚点 → 8 个 distinct PC,addr2line 全部命中真实函数:
`_Z3addii`(add)、`_Z8loop_sumi`(loop_sum)、`_Z5setupv`(setup)、`TIM8_UP_TIM13_IRQHandler`。
orbetm 从锚点出发能精确重建 add() 的三指令序列 `mov r2,r0; adds r0,r2,r1; bx lr`。**这些是
物理实测确认的真值。**

### 10.3 不可信的部分:锚点之间的连续流(诚实结论)

orbetm 报"最长连续 342 指令"——但 `-v` 详查发现这是**假象**:loop_sum 的 `mov r0,r3`(0x8000fb0)
之后跳到 0x08000f0a 并在 0x08000f0a..0f26 反复打转(出现 `movs r0,r0`=0x0000 这种"走进数据"
的标志)。根因:**间接返回(`bx lr`/`pop pc`)在流里没有目标地址**——branch broadcast 只补了
*直接*跳转的目标,间接跳转仍依赖调用栈;而硬件 1024 字节 I-sync 周期之间一旦栈不完整,walk 就
失准,又因为没离开 flash 窗口而"看起来"在连续走,把 run 长度灌水了。

→ 已在 orbetm 的 `endRun` 注释里如实标注:**run 长度不是"已验证正确"的跨度;只有 I-sync 锚点
是真值,锚点间的流仅供参考。**

### 10.4 杠杆已用尽 —— 这就是 ETM-M4 的物理天花板

- branch broadcast:ON(已是最优,7× 于 OFF)。
- ETM 同步周期 ETMSYNCFR:硬件只读锁死 1024 字节,动不了。
- 地址比较器:ETM-M4 有 0 个(ETMCCR 实测),无法用比较器强制更密的地址输出。

**三个旋钮全部用尽。** 在这颗 STM32F429 的 ETM-M4 上,中速完整 trace 能做到的极限就是:
**密集 I-sync 锚点(每函数级别可信)+ 锚点间短程指令重建**,而非全程逐指令无错。要全程无错的
连续逐指令流,需要的是带更多比较器 / 可调同步周期 / 时间戳关联的更高端 trace 单元(商业
J-Trace / ULINKpro 级),这是硬件能力差异,不是软件或采样能补的。

### 10.5 结论(对齐终极目标)

- **台阶①(连续逐指令解码)在 ETM-M4 上的可达上限 = 函数级可信 + 短程指令流**,已达成并诚实定界。
- 全程逐指令无错是该硬件的天花板之上,不是当前方案能突破的;这点必须如实告诉决策者,避免在
  无法逾越的物理限制上继续投入。
- 真正可继续推进的是台阶②/③(满速 + 不丢传输),那是"采到更多原始数据"的方向,与"解码"无关。

---

## 11. 重要更正:§10 的"硬件天花板"结论是错的 —— 这是解码软件问题

用户质疑得对:"J-Trace 也是 FPGA 做的,凭什么有硬件能力差距?" 复查后 §10.4/§10.5 的
"ETM-M4 物理天花板"结论**撤销**。真相是这是**解码软件的调用栈问题,完全可修**。

### 11.1 决定性证据:质量按"距上一个好锚点的距离"分桶

把 orbuculum 解码出的地址按"距上一个 in-code 地址多远"分桶,统计落在代码区的比例:

| 距好锚点 | 地址数 | in-code | 占比 |
|----------|--------|---------|------|
| 0(紧跟好地址) | 23069 | 22750 | **99%** |
| 1 | 319 | 0 | 0% |
| 2..11 | 各 319 | 0 | 0% |

**紧跟好地址的下一个地址 99% 还是好的;然后恰好"一个"坏地址(319 次 = 正好等于 run 数),
之后就一路跑飞直到下个锚点重新拉回。** 这不是硬件采不到,是**解码器在某一类指令上失同步**。

### 11.2 失同步点 = 间接返回(`pop {pc}` / `bx lr`)

`-v` 详查:每个 run 的终结指令统计——**81/83 是 `pop {r4, pc}`**(间接返回)。根因是经典的
**返回地址/调用栈**问题:间接返回的目标不在代码镜像里,解码器必须靠"调用时压栈、返回时弹栈"
来跟踪。J-Trace/ULINKpro 的解码软件就是这么干的——**和我们用的是同一颗 ETM、同一类 FPGA 采集**,
差别只在 PC 端解码器维护了正确的调用栈。

### 11.3 修复 orbetm:间接分支后停下等地址包

之前 orbetm 在间接分支后用栈候选"猜"一个地址就继续走 atom,猜错就跑飞。改为:**间接分支后立即
停止当前 atom run,等下一个 Branch Address 包(broadcast ON 必然带真实目标)经 EV_CH_ADDRESS
拉回**。修复后 `-v` 输出已是连贯的真实控制流(`pop pc → push {r4,lr} → ldr → bl 0x8000fc2 →
pop {r4,pc}`),~91% 是真实指令流,残留 ~9% 是栈候选偶尔过期的重复 pop(可继续收敛)。

### 11.4 更正后的结论

- **全程逐指令连续 trace 在这颗 ETM-M4 上是可达的,瓶颈在 PC 端解码器的调用栈跟踪,不是硬件。**
- 路径明确:把 orbetm 的调用栈/返回匹配做扎实(对齐 orbmortem 的 `stackDelPending` 提交语义,
  并处理递归/中断导致的栈失衡),连续段就能从"锚点间短程"扩到"跨函数全程"。
- 我之前下"物理天花板"是信息不全时的过强结论(只看了 in-code 占比 27%,没按距离分桶就归因硬件)。
  分桶一看就清楚:99% 的局部正确率证明数据是好的,是解码没跟住。**保留此教训。**


---

## 12. OpenCSD 摸底(只评估,未决策)

用户问"有 OpenCSD 还要不要手搓 / 有没有现成 CLI / 谁在用这些库 / orbuculum 为啥不用它 / 以后要
流式怎么办"。先把 OpenCSD 摸清楚再决定。

### 12.1 OpenCSD 是什么 / 谁在用

- **ARM/Linaro 官方开源 CoreSight 解码库**,Linux 内核 `perf`(`perf record -e cs_etm//`)的
  CoreSight 后端,LLVM AutoFDO、Google 数据中心 ARM PGO 都用它。装机量 = Cortex-A Linux/安卓
  几十亿台。这就是"谁在用"——**主战场是 Cortex-A,trace 存片内 ETR/ETB,软件读出,不接探针**。
- 我们用的是它的边角:**ETMv3.5 / M-profile 解码**(同一套 ETM 协议,A/M 复用)。

### 12.2 为什么 orbuculum 不用 OpenCSD(推断,无作者背书)

- orbuculum = **实时流式 + 纯 C + 轻量**(逐字节 pump,边抓边显示,orbtop 实时视图)。
- OpenCSD = **offline/batch + C++ 重库 + snapshot 输入**(攒完整 dump 再一把解)。
- 两者数据流模型正交;且 OpenCSD 早期重心在 ETMv4/Cortex-A,M-profile 支持是后补的。
- **对我们 offline 对拍而言,OpenCSD 才是主场;对"以后实时一直抓"而言,orbuculum 路线才对。**

### 12.3 现成 CLI:有 —— `trc_pkt_lister`(Ubuntu 有包)

- `apt install libopencsd-bin libopencsd-dev`(已装,**1.4.1**,零编译)。
- `trc_pkt_lister -ss_dir <snapshot> -decode` 做完整 ETMv3.5 指令解码;带 `-tpiu` 选项可直接吃
  TPIU 帧(连填充都不用我们剥)。
- **门槛**:输入是 OpenCSD "snapshot 目录"(ini 配置 + 原始 trace dump + 内存镜像),不是裸 bin。
  需要写一个**纯打包脚本**(不碰解码逻辑)把我们的数据包装进去。

### 12.4 snapshot 格式(已读官方 spec,打包很简单)

`/usr/share/doc/libopencsd-dev/specs/ARM Trace and Debug Snapshot file format 0v2.pdf`:
- `snapshot.ini`:[snapshot] version=1.0 + [device_list] + [trace] metadata=trace.ini
- 一个 core 设备 ini(class=core, type=Cortex-M4)+ 一个 trace 源 ini
  (class=trace_source, type=**ETM3.5**)
- **ETMv3 解码只需 4 个寄存器**:`ETMCR, ETMCCER, ETMIDR, ETMTRACEIDR` —— 我们全有/可从靶子读。
- core ini 用 `[dump]` 指向内存镜像(直接用 ELF 的 .text;spec 明说可以是 elf 可加载段)。
- `trace.ini`:[trace_buffers] format=source_data(我们已剥成裸 ETM 单源)或 coresight(带 TPIU)。

→ 打包脚本 = 写 3 个小 ini + 把 `/tmp/dsl_bytes_0.bin` 和 ELF 放进目录。**几十行 Python,零解码代码。**

### 12.5 流式("以后一直抓")的定位

流式 = 两段独立问题:
1. **连续不丢传输(台阶②/③,FPGA 侧硬骨头)**:逻辑分析仪是快照设备,不能无限流;要"一直抓"
   必须 FPGA 实时收 ETM → orbflow 打包 → 千兆网口连续吐 + 序号防丢。这跟解码无关。
2. **实时解码显示**:这段用 **orbuculum**(它本就是实时流式),不是 OpenCSD。

**结论性认识**:offline 对拍/求最准 → OpenCSD;实时一直抓 → orbuculum 路线;两者解码内核同协议,
现在用 OpenCSD 把"调用栈/持续流的理论上限"摸到底,认知可直接迁移到实时管线的取舍。

### 12.6 现状

OpenCSD 1.4.1 已装(`trc_pkt_lister` + C/C++ 库 + 头 + 官方 spec/HOWTO 齐全)。**下一步(待用户拍板)
= 写 snapshot 打包脚本,跑 `trc_pkt_lister -decode`,得到 ARM 官方解码器在我们 M4 流上的持续指令流
质量——这是"调用栈能恢复多少"的权威标尺。** 未决策,先摸底完成。


---

## 13. 跑了一把 OpenCSD —— 权威标尺出来了

写了 `make_opencsd_snapshot.py`(纯胶水,零解码代码)把我们的裸 ETM 流 + ELF + 实读的 4 个
ETMv3 寄存器(ETMCR=0x980 ETMCCER=0x18541800 ETMIDR=0x4114f250 ETMTRACEIDR=0x02)包成 OpenCSD
snapshot,跑 `trc_pkt_lister -decode`。

### 13.1 OpenCSD 确实能解我们的流(packet 级全对)

`trc_pkt_lister`(不 decode,只列 packet)从 A-sync 对齐后,正确解出 A_SYNC / P_HDR(EE/EEEE/
EEEEEEEE)/ BRANCH_ADDRESS / TRIGGER —— **ARM 官方解码器认我们的流,packet 解析全对。**

### 13.2 指令级解码:锚对了就对,但有两个坑

1. **ISA=Thumb 必须靠"带 Thumb 位的 I-sync"锚定**。IHI0014Q 实证:I-sync 地址 bit[0] 是 Thumb 位
   (Thumb 态=1)。我们流里 334 个 flash I-sync:**160 个 bit0=1(真 Thumb 锚点),174 个 bit0=0
   (多半是 mid-stream 假阳性 0x08)**。从 bit0=1 的 I-sync 锚定,OpenCSD 立刻 `ISA=T32` 正确解出
   `exec range=0x8000e90 ... (ISA=T32) E` —— 真实 Thumb 指令范围。从 A-sync(没跟 I-sync)起步则
   默认 A32,解错。
2. **OpenCSD 是"对或中止"(correct-or-abort)**:遇到一个它不认的 packet(我们锚点间的噪声/
   reserved 头)就**直接终止整条 datapath**,不像 orbuculum/orbetm 那样硬着头皮往下走。实测:
   从一个好 Thumb 锚点起,OpenCSD 解出**恰好 1 条指令范围**就在下一个坏 packet 处中止;整条流从头
   跑则在第 2593 字节中止(把噪声误判成 data-trace,报 OCSD_ERR_HW_CFG_UNSUPP)。

### 13.3 决定性结论:工具特性 × 数据特性的错配

| | OpenCSD(ARM 官方) | orbuculum / orbetm |
|---|---|---|
| 设计目标 | Cortex-A,**片内 ETR/ETB 无损 trace** | Cortex-M,实时/容错 |
| 坏 packet | **中止整条解码** | 跳过/续走 |
| 在我们这条**有损隙的逻辑分析仪流**上 | 锚点处完全正确,但一遇噪声即停 | 一路走完(但锚点间会跑飞) |

**这把标尺量出的真相**:我们流的瓶颈不在解码器算法,而在**流本身有"锚点间噪声/不连续"**——
OpenCSD 这种为无损片内 trace 设计的严格解码器,直接拒绝在脏流上硬解(这恰恰是它工业级正确性的
体现:它不会给你编造的指令流)。orbuculum 容错往下走,代价是锚点间不可信。

→ **两边都不是 bug,是数据脏**。要让任一解码器吐出长段连续可信指令流,**必须先有"干净不丢的流"**
——这就把球又踢回**台阶②/③(FPGA 侧无损连续采集)**。换句话说:**OpenCSD 这把权威标尺证明了
"解码不是瓶颈,采集质量才是"**。我们之前在解码侧反复纠结(自研/复用/调用栈),方向上是次要矛盾;
主要矛盾是把 STM32 ETM 无损连续地搬进 PC。

### 13.4 产出

- `make_opencsd_snapshot.py`:`.bin`+ELF → OpenCSD snapshot(纯胶水),固化下来随时可复跑。
- 结论:**offline 求最准用 OpenCSD(但要喂干净流);现状脏流下 orbuculum 容错更实用;真正要突破
  连续性,回台阶②/③ 做无损采集。** 解码工具选型到此尘埃落定,不再纠结。


---

## 14. dsl→OpenCSD 对拍归因:逻辑分析仪采集是干净的,问题不在采集

用户要求:把 dsl 喂 OpenCSD,如果逻辑分析仪采得没问题、是 FPGA/处理侧的问题,就专攻后者直到对齐。
做了 `opencsd_region_probe.py`(从每个真 Thumb 锚点独立喂 OpenCSD)+ 把 OpenCSD 中止点的字节
**映射回原始 DDR 采样**做眼图裕量检查。

### 14.1 OpenCSD 每个锚点只解 ~14-16 字节就中止

160 个真 Thumb 锚点(addr bit0=1),OpenCSD 从每个出发平均解 ~15 字节(I-sync 6B + 约 1 条指令
范围 + 几个包)就遇到不认的包中止。中止点的典型字节序列(锚点 0x08000e91 之后):
```
84 01 37 | 00 58 05 90 2e 8c 0c 8c 36 ...
P-hdr(E) branch  ???
```

### 14.2 ★ 决定性:中止点的字节在原始波形上"眼图全开",采集 100% 干净

把中止点每个字节映射回 DDR 采样,测每个 nibble 在采样点附近的稳定样本数:

| byte | 值 | 眼图稳定样本数(低/高 nibble) |
|------|----|------------------------------|
| 6741 | 0x84 | 18 / 18 |
| 6743 | 0x37 | 18 / 18 |
| 6744 | 0x00 | 37 / 18 |
| 6745 | 0x58 | 18 / 37 |
| 6748 | 0x2e | 18 / 18 |

**半周期 = 19 样本,每个 nibble 稳定 18-37 个样本 = 整个半周期纹丝不动。** 加上之前测的
"时钟 0 毛刺/0 丢沿"、"18-bit 最大连 1(与 TPIU HSYNC 一致)",**逻辑分析仪在低速把每个 bit 都
干净采下来了——采集侧没有任何问题,不是 SI,不是采样相位。**

### 14.3 那问题在哪:字节级解释(解码覆盖 / 流结构),不在采集

中止点的字节(`00 58`、反复出现的 `2e 36 3a 16 1c`)是 STM32 **真实吐出的**字节(实测眼图证明),
但作为裸 ETM 它们让 OpenCSD 和 orbuculum 都判为非法包。三种可能(待进一步定位,但都**与采集质量
无关**):
1. F429 TPIU 是 **HSYNC-only 连续格式**(实测:53667 个 `FF 7F` 半同步,**0 个 `FFFFFF7F` 全同步**,
   最大连 1 只有 18 位)。我们 `strip_tpiu_sync` 剥掉 `FF 7F` 后,可能破坏了 TPIU 字节对齐
   (TPIU 是 16 字节帧,剥字节会移位)。
2. 这些 `2e/36/3a` 可能是某类我们和 orbuculum 都没处理的合法 ETM 包的头/续字节。
3. 174 个 bit0=0 的假 I-sync 说明流里 0x08 歧义,暗示对齐仍有残留问题。

### 14.4 结论与下一步(锁定了正确战场)

**采集干净 = 已证实。** 按用户方法论,问题不在逻辑分析仪/硬件采集,**专攻"字节级流处理"**:
- 最可疑的是 #1:**HSYNC-only TPIU 的正确去帧**。我们一直当"裸 ETM + 剥填充",但 F429 实际是
  HSYNC 连续格式,正确做法可能是按 TPIU 16 字节帧去 mangle(OpenCSD 的 `-tpiu_hsync` 正是为此),
  而不是简单删 `FF 7F`。
- 下一步:用 OpenCSD `-tpiu_hsync` / 正确的 TPIU HSYNC 去帧重试,看锚点后能否连续解完整周期。
- 这一步打通后,FPGA 侧 trace_stream/orbflow 的字节流就能和逻辑分析仪这条"已证采集干净"的基准
  做对拍,任何 FPGA 侧偏差立刻可见——这正是用户要的"两个性能完全对齐"的标尺。

**一句话:逻辑分析仪低速采集已证 100% 干净;瓶颈是 PC 端对 F429 HSYNC-only TPIU 流的去帧/对齐,
不是采集,也不是 SI。提频做阻抗板的事要往后放,先把这个字节级处理打通。**

### 14.5 续:更正 #1 的猜测 —— 不是 16 字节 TPIU 帧问题

进一步实测把 14.3 的可能 #1(TPIU 16 字节去帧)**排除**了:

- **流不是 16 字节 TPIU 帧**:334 个 flash I-sync 的偏移 mod 16 **均匀散布在 0..15**(若是帧,
  会被帧结构约束);暴力试 16 个帧相位去帧,最好的也只恢复 14 个 I-sync(远不如简单剥 `FF 7F` 的
  334)。**无 FSYNC(0 个 31 连 1)、无帧结构** → 这是**裸 ETM + 周期性 HSYNC 填充**,不是分帧流。
  简单剥 `FF 7F` 是对的。
- `tpiu_deframe`(orbuculum 16 字节去帧)在此流上跑不动/不收敛(无 FSYNC 锁不住帧),证实同上。

### 14.6 ★ 采集干净的第二个铁证:确定性

proj_add 是 `while(1){loop_sum(5);}` 紧循环。统计同一个 I-sync 锚点(0x08000e91)后面紧跟的
16 字节窗口,**157 次重复只出现 3 种,头两种占 156/157(79+77,循环的两个相位),只有 1 次异常**。
**高度确定性 = 采集干净**(噪声会让 157 次各不相同)。加上 14.2 的眼图铁证,**采集干净双重坐实**。

### 14.7 真正的卡点(已精确定位,但属解码侧未解)

OpenCSD 从真 Thumb 锚点出发,完美解出 A_SYNC → I_SYNC(0x08000e90,Thumb2)→ P_HDR(E)→
指令范围 → BRANCH(0x01→e80)→ BRANCH(0x37→eb6),**然后遇到 `0x00` 报
`BAD_SEQUENCE: Invalid sequence [A_SYNC]` 中止**——它把孤立的 `0x00` 当成 A-sync 开头,但后面
不是连续的 0。这个 `0x00 0x58 ... 2e ... 36` 序列是**真实、确定性、眼图干净**的捕获数据
(raw +9 处,附近无 HSYNC 剥除),反复出现 79 次。

`2e/36/3a/16/1c` 这组反复出现的"未知"字节(占流 16%)**既不是合法 ETM3.5 IDLE 包头,也不是噪声**。
最可能是:**两个 branch 之后的某个 ETM 包(或 P-header 序列)里,我们和 OpenCSD 对 packet 长度/
类型的理解,与 F429 ETM 实际输出有出入** —— 这是**解码侧**问题,但已经和采集彻底解耦了。

### 14.8 当前结论(把战场钉死)

| 层面 | 状态 |
|------|------|
| 逻辑分析仪硬件采集(bit 级) | ✅ 100% 干净(眼图 + 确定性双证)|
| TPIU 16 字节去帧 | ❌ 不适用(此流是裸 ETM+HSYNC 填充,非分帧)|
| 剥 `FF 7F` HSYNC 填充 | ✅ 正确(恢复 334 真锚点)|
| 锚点本身 | ✅ 全部解出真实函数 PC |
| 锚点后连续解码 | ⚠️ ~1 条指令即遇 `0x00...` 中止 —— **解码侧待解**,与采集无关 |

**给用户的决策依据**:你说的"逻辑分析仪采得没问题就专攻另一侧"——**采得确实没问题(已双重证明)**。
但"另一侧"经精确定位**不是 FPGA**,而是 **PC 端对这段确定性 ETM 字节流(尤其 `00/2e/36` 模式)的
解码理解**。这是纯软件、可离线复现的问题。FPGA 对拍要等这个解码理解打通后才有意义(否则拿一个
我们自己都解不通的基准去对 FPGA,无法判定对错)。

**下一步(待批)**:聚焦搞懂 `84 01 37 00 58 05 90 2e ...` 这段确定性序列的真实 ETM 语义——
对照 IHI0014Q 把 `0x00`/`0x2e`/`0x36` 这组字节的包类型彻底搞清,这是解开"锚点后连续解码"的钥匙,
也是建立可信基准、再去和 FPGA 对拍的前提。


---

## 15. 逐字节啃 ETM 文档:两条强结构线索(未结案,但缩小到点)

按用户要求逐字节对 IHI0014Q,不猜。确认 M4 无 data trace(DDI0440C §2.1.1 + ETMIDR bit16=0),
所以流里落在 data 编码空间的字节必是错的或被误解的。两条强线索:

### 15.1 字节字母表(全流统计)

| 类别 | 量 | 主要值 |
|------|----|--------|
| pheader | 476806 | 8c(21万) 90(10万) 88(6万) cc 94 ba ee d6 |
| branch | 452930 | 05(17万) 01 37 0d 2f 25 1d 17 |
| **unknown(data空间)** | **194469 (16%)** | **2e(8.4万) 36 3a 16 1c 6a 68 58** |
| async 00 / trigger 0c | 2万/4.8万 | |
| isync 08 / **isync+cyccnt 70** | 1636 / **8310** | |
| exc_exit 76 / contextid 6e / timestamp 46 / vmid 3c | 少量 | |

### 15.2 ★ 线索一:每个"未知"字节 = 某合法 branch 字节的 bit0 翻 0

8 个未知值无一例外:`2e=2f^1, 36=37^1, 3a=3b^1, 16=17^1, 1c=1d^1, 6a=6b^1, 68=69^1, 58=59^1`。
全部是"branch 字节(bit0=1)把最低位清成 0"。bit0 = TRACED0(D1 线)在低 nibble 边沿的采样。
**但若 TRACED0 真坏,45 万个正常 branch(bit0=1)不该存在** → 不是死线,排查方向是某种条件性
bit0 错误,但**与边沿类型(R/F)无关**(实测两类各占,无偏)。

### 15.3 ★ 线索二:每个"未知"字节都紧跟在一个 P-header 之后

实测窗口里 `90→2e, 8c→36, 88→1c, 90→16, 88→24, 94→3a, 88→2e` —— **"未知"字节总是紧跟 P-header**。
但规范(Table 7-2/7-3)明确 **P-header 永远是 1 字节、无 payload**(cycle-accurate 模式也是)。
所以 `P-header + 未知字节` 不是一个合法 2 字节包。这个"P-header 后必跟一个 bit0 被清的 branch"的
强规律,是解开谜题的钥匙,但还没对上规范里任何机制。

### 15.4 已排除

- 不是 cycle-accurate 误配:给 OpenCSD 设 ETMCR bit12=1 重解,P-header 变 WEWE 解释,但 `2e/58/36`
  仍被拒(同样位置中止)。
- 不是 HSYNC 剥除错位:未知字节在原始流里并不紧邻 `FF 7F`。
- 不是 nibble/bit 整体错序:整体 bit-reverse / nibble-swap / 4-bit 移位都更差(0 锚点)。
- 不是采集脏:眼图 + 确定性双证干净。

### 15.5 当前最可能假设(待验证,排序)

1. **bit0(TRACED0/D1 线)在 P-header 之后的那一拍存在条件性采样错误**——比如 P-header 那拍的
   TRACED0 跳变与时钟边沿存在固定相位关系,导致紧跟其后的字节 bit0 被采错。这能同时解释线索一
   (bit0 翻转)和线索二(总在 P-header 后)。**需要回到原始波形,专看"P-header 字节→下一字节"
   交界处 TRACED0 的跳变时刻 vs 采样点**。
2. F429 ETM 在 P-header 后真的插了某种我们没识别的合法包,且恰好 bit0=0——但规范里对不上,可能性低。

### 15.6 给研究者的文档地图(IHI0014Q + DDI0440C)

- IHI0014Q **§7.2 Table 7-1 包头编码**(行 16520)= 字节字典,最常翻。
- IHI0014Q **§7.3.1 P-header / Table 7-2(行 16684)、Table 7-3 cycle-accurate(行 16723)**。
- IHI0014Q **§7.3.5 Branch Packets(行 16936)、§7.10 Synchronization(行 19580)**。
- IHI0014Q **§7.4 Data tracing**(`2e/58/68` 落点,M4 不该有)。
- DDI0440C **§2.1.1 Resources(行 681)/ ETMIDR bit16(行 1536)/ 资源表(行 778)**:M4 无 data trace 实证。

### 15.7 下一步

回原始 DDR 波形,**专测 P-header 字节与其后字节交界处 TRACED0(D1)的跳变沿相对采样点的位置**,
验证假设 1(条件性 bit0 采样错误)。若坐实,是采样相位/eye 中心的精修问题(可能 per-line deskew),
**这才可能最终指向 SI / 连线** —— 但要先用波形证据锁死,不是现在就下结论。

### 15.8 ★★ 重大更正:假设 1 也被波形推翻 —— 这些字节是真实的

实测 P-header 后那个"未知"字节的低 nibble 采样点,**TRACED0(bit0)整段眼图稳定为 0**
(`000000000`,不是边沿抖动)。即 **bit0=0 是线上真实值,采集完全正确**。所以:

- ❌ 不是采样错(TRACED0 真的是 0)
- ❌ 不是 bit0 条件性翻转
- ✅ **`0x2e`/`0x36` 等字节是 STM32 ETM 真实吐出的字节**

而 `0x2e` 按 IHI0014Q Table 7-1 **无歧义就是 "Normal data" 包头**(b00A0SS10)。于是形成硬矛盾:
**真实在线、眼图干净的字节 = 数据包头;但 DDI0440C 明说 M4 ETM 不发数据包。**

唯一出路:**这些字节没有被在正确的包边界上解读** —— 即便每个 bit 都对,我们把某处的"包起点"
认错了,导致本应是别的东西的字节落在了"数据包头"位置。但 P-header 恒为 1 字节(已查 Table 7-2/7-3),
`unknown/pheader` 比为 0.41(非 1:1,"总在 P-header 后"只是局部现象)。**真正的包边界错位机制仍未找到。**

### 15.9 诚实的当前状态(给一起研究的人)

- **采集 100% 干净**(眼图 + 确定性 + 单字节波形,三重证明),逻辑分析仪/硬件无责。
- 字节流里 16% 是落在 data 编码空间的真实字节(`2e/36/3a/16/1c/6a/68/58`),M4 理论上不该发。
- **谜题收敛到一点**:为什么真实、确定性的字节流里有大量"数据包头"?三种待查方向:
  1. **F429/Cortex-M4 ETM 的实际行为与 DDI0440C 表述有出入**(某些 STM32 ETM 版本/配置会发我们
     没预期的包)——需查 STM32F429 勘误表 / STM32 ETM 应用笔记 / 实际 ETMIDR 版本号。
  2. 我们对 ETM **包序列的消费长度**在某个变长包上系统性错位(虽然 P-header 是 1 字节,但某个
     branch / cyccnt / I-sync+cyccnt(0x70,8310 个!)的长度我们算错,导致后续整体移位)。
     **0x70(I-sync with cycle count)有 8310 个但 ETMCR cycle-accurate=0,这本身就矛盾,是重点嫌疑。**
  3. 真有 data trace(固件/调试器开了 DWT→ETM 的某条路径)。
- **最该先查**:`0x70`(I-sync+cyccnt)为何在非 cycle-accurate 配置下大量出现 —— 若它其实不是
  I-sync+cyccnt,而是我们误判的别的东西,其"长度"算错就会让整条流错位,批量制造假数据包。
  这是目前最强的单一嫌疑点。


---

## 16. ★★★ 谜题解开:流是真 16 字节 TPIU 帧,正确去帧后连续解码 1→210 指令

### 16.1 根因(sigrok arm_tpiu 定义交叉验证)

§15 的"数据包头"谜题解开了。关键线索"每个未知字节 = 某 branch 字节 bit0 清零"正是 **CoreSight
TPIU 16 字节帧格式**的签名(sigrok arm_tpiu 定义):

> 16 字节帧。偶数字节(0,2,..14):bit0=1 → 流 ID;bit0=0 → 数据,**其真实 LSB 存在帧第 15 字节**。
> 奇数字节恒为数据。第 15 字节 = 各偶数据字节被抽走的 LSB。

所以 `0x2f`(branch)落在偶数位时,LSB 被抽走变成 `0x2e`,真 LSB 进第 15 字节 —— 这就是那 16%
"伪数据包"的来源。**我之前的 `strip_tpiu_sync`(只删 FF 7F)是错的**:它没处理 16 字节帧结构,
更没恢复偶字节 LSB。

### 16.2 正确去帧 = `tpiu_deframe_hsync`

新增 `etm35lib.tpiu_deframe_hsync`(+ `find_tpiu_phase`),做三件事:
1. **跳过 HSYNC(FF 7F)/ FSYNC(FFFFFF7F),且不占帧字节槽**(关键:删字节会错位,跳过才对)。
2. 按 16 字节切帧,偶字节从第 15 字节**恢复 LSB**,奇字节直取,流 ID 字节剔除。
3. 无 FSYNC 锁相时,扫 16 个起始相位,取 flash 锚点最多的。

效果(194702 capture):**"未知"字节 16% → 0.0018%**;flash 锚点 334 → **818**;`0x70`(假
I-sync+cyccnt)、假 contextid/timestamp/vmid **全部消失**(证实它们都是帧结构假象)。

### 16.3 OpenCSD 连续解码:1 → 210 指令

正确去帧后喂 OpenCSD(ARM 官方解码器),每锚点连续解码:

| | 之前(错误 strip) | 现在(正确去帧) |
|---|---|---|
| 每锚点指令数 | ~1(max 5) | **mean 45,median 24,max 210** |
| 每锚点解码字节 | ~15 | mean 41,max 139 |

**且控制流经 ground-truth 核对完全正确**:add 的 `f8c→f8e→f90(bx lr, V7:impl ret)`,返回到
loop_sum 的 `fb6`,`fbc blt <cond>`,`f96/fb2 b+link`(bl add)—— **loop_sum 调 add 的完整
循环被逐指令重建**,210 条连续无错。

### 16.4 意义

- **解码侧从此打通**:用 ARM 官方 OpenCSD,正确 TPIU 去帧,proj_add 中速连续逐指令重建已实现。
- 之前所有"锚点间跑飞/数据包/调用栈"的困扰,**统一根因 = TPIU 去帧错误**,现已修复。
- `dsl_parse.py` 已固化:自动定 nibble 对齐 → 检测 TPIU → 扫相位正确去帧 → 写干净 ETM 流。
- 残余的"abort"是 OpenCSD 严格性 + region probe 喂孤立片段所致;喂全流让其在每个 I-sync 自重锚
  可进一步拉长。剩余 0.0018% 坏字节是采样/边界的极少数毛刺,不影响主结论。
- 134 个单测通过(新增 5 个 TPIU 去帧用例)。

### 16.5 这对"对齐 FPGA"意味着什么

现在有了**一条端到端可信的基准**:逻辑分析仪 .dsl → 正确去帧 → OpenCSD 连续指令流(已对 ground
truth 验证)。**FPGA 侧 trace_stream/orbflow 的字节流,只要也做同样的 TPIU 去帧,就能和这条基准
逐字节/逐指令对拍** —— 用户要的"两个性能完全对齐"的标尺,现在真正立起来了。


---

## 17. 参考流"残余 15 个未知字节"查清:是 SysTick 异常信息字节,流本身 100% 干净

FPGA 对拍前先把 LA 参考流的最后 0.0018%(15 个未知字节)查清(否则标尺不干净)。结果:

- 15 个全是**同一个字节 `0x1e`**,且都在**同一上下文 `bd 57 1e 98 76`**,周期性出现(每 ~5.4 万字节一次)。
- 解码:`0x57` = branch 包,`0x1e` = 该 branch 的**异常信息字节**(IHI0014Q §7.3.5 Exception
  Information Byte):C=0 Alt=0 Can=0 **Exception=15** NS=0。**异常号 15 = SysTick**。`0x76` = 异常退出。
- 即:这是 **SysTick 中断的进入(branch+异常信息)/退出**序列,在整段抓取里正好出现 **15 次**
  (`571e` 15 个、`0x76` exc-exit 15 个,完全吻合)。

**所以 `0x1e` 不是未知字节,是合法的 branch 异常信息载荷字节。** 我们的 `_classify` 只识别包
**头**、不建模 branch 包的异常信息**载荷**,才把它误报为 unknown。**TPIU 去帧后的 ETM 流本身
100% 干净,0 个真未知字节。** 参考标尺干净,可以拿去和 FPGA 对拍。

## 18. FPGA 对拍:工具就绪,待硬件上电

### 18.1 对拍原理

FPGA `trace_stream` 和逻辑分析仪看的是**同一组 STM32 trace 引脚**:
- LA 路:`.dsl → dsl_parse(DDR对齐)→ tpiu_deframe_hsync → ETM 字节`
- FPGA 路:`trace_stream UDP dump(原始 TPIU 字节)→ tpiu_deframe_hsync → ETM 字节`

FPGA 采集前端若正确,两条**去帧后的 ETM 流应解出相同的 flash I-sync 锚点集合与相同指令流**。

### 18.2 工具

`fpga_la_crosscheck.py`:对两路做同样去帧+解码,报告各自去帧洁净度、锚点集合、重叠/分歧,
判定 MATCH / DIVERGENCE。已自测 LA 路复现 818 锚点。

### 18.3 待办(需 A7-Lite 上电联网)

当前 FPGA 不在线(192.168.10.42 ping 100% 丢包)。对拍需要:
1. A7-Lite 上电、烧 `trace_stream.bit`(`scripts/program.sh stream jtag`)
2. 跑同一 proj_add 固件 + 开 ETM(`scripts/etm_enable.sh`)+ 降频
3. arm 一次性采集 + UDP dump(`scripts/trace_run.sh` 或 `capture.sh`)→ 原始 TPIU 字节
4. `python3 fpga_la_crosscheck.py <fpga_raw.bin> <la.dsl>` → 看是否 MATCH

目标:FPGA 去帧后 **0 未知字节、锚点集合与 LA 完全一致**,即两路 100% 对齐。


---

## 19. FPGA 对拍首跑:DIVERGENCE —— FPGA 采集前端有 bug,LA 是干净基准

重新上电后:重配 ETM(ETMCR=0x980 branch-bcast ON,HCLK /64)、刷 `trace_stream.bit`,
UDP dump 36864 字节(full=1,36279/36864 非零,数据在流)。跑 `fpga_la_crosscheck.py`:

| | FPGA(trace_stream) | LA(.dsl 参考) |
|---|---|---|
| ETM 字节 | 26909 | 851428 |
| flash 锚点 | **9(全是垃圾地址)** | **818(全在 add/loop_sum)** |
| unknown 字节 | **17.8%** | 0.0018% |
| 锚点重叠 | **common=0** | — |

**判定:DIVERGENCE。** FPGA 解出的锚点如 `0x801ffbc`、`0x8002d90`、`0x802f42c` —— 注意比
LA 的 `0x8000fbc` **多一位十六进制(地址被插入了多余 nibble)**,典型的 nibble/位级错位。
试过整体 nibble 移位、字节内 nibble 交换都救不回(都归零),说明 FPGA 采集前端
(`trace_capture_a7` / `traceIF.v`)的字节组装与真实引脚序**结构性不一致**,不是简单一个相位。

### 19.1 这正是对拍的价值

- **LA 路 = 已证 100% 干净的金标准**(818 锚点、0 真未知、控制流逐指令对 ground-truth)。
- **FPGA 路 = 实测有 bug**(17.8% unknown、0 个锚点与 LA 重合、地址多 nibble)。
- 对拍工具一次就把"FPGA 采集前端坏了"钉死,且给出具体症状(地址多 nibble = DDR 重组错位)。

### 19.2 FPGA 侧待修(下一阶段)

FPGA `trace_stream` 经 `trace_capture_a7.v`(IDELAYE2+IDDR DDR 采样)+ `traceIF.v`(TPIU 锁同步
+组帧)。症状"地址多 nibble"指向 **DDR 双沿 nibble 拼装顺序 / IDELAY tap / traceIF 的同步对齐**
与 STM32 实际 4-bit 口输出不匹配。修复方向:
1. 对照 LA 已验证的正确 nibble 映射(rise=低 nibble、parity),核对 `trace_capture_a7` 的
   IDDR Q1/Q2 → trace_a/trace_b 拼装顺序。
2. 用同一把 `fpga_la_crosscheck.py` 迭代:每改一版 FPGA,dump → 对拍,直到 unknown=0、锚点集合
   与 LA 完全一致。

### 19.3 工具产出

- `fpga_la_crosscheck.py`:FPGA raw vs LA .dsl,同样去帧+解码,报洁净度/锚点/重叠/判定。
- 目标量化指标:**FPGA 去帧后 unknown=0,锚点集合 ⊇ LA 的 add/loop_sum 集合**,即两路对齐。


---

## 20. FPGA 对拍迭代:IDDR 模式修正 + 发现默认抓的是 traceIF 帧(非原始引脚)

### 20.1 DDR 采集是自写,根因之一是 IDDR 模式

`trace_capture_a7.v` 是**自写**的(orbtrace 原版只支持 ECP5 的 IDDRX1F,经 LiteX `DDRInput`
抽象;Xilinx 7 系无法直接搬,故手写)。查 LiteX 源码 `XilinxDDRInputImplS7`:它把 `DDRInput`
降级为 **IDDR `DDR_CLK_EDGE="SAME_EDGE"`**,Q1=o1(上升),Q2=o2(下降)。**我们误用了
`SAME_EDGE_PIPELINED`**,其 Q1/Q2 配对相对周期边界偏移一拍,交换了上升/下降 nibble 关联 —— 这是
nibble 错位的根因之一。已改回 `SAME_EDGE`(commit b4390d9),对齐 orbtrace 验证过的行为。

### 20.2 但对拍仍 DIVERGENCE —— 发现第二个、更根本的不匹配

改 IDDR 后重抓对拍仍分歧(14.8% unknown)。根因:**默认 `trace_stream_top` 用 `CAP_RAW=0`,抓的
是 `traceIF.v` 片上 TPIU 锁帧后的 16 字节帧**,不是原始引脚 nibble。而 LA 抓的是原始引脚。
两者表示层根本不同(FPGA 是 traceIF 处理过的帧,LA 是裸 TPIU 字节),没法直接对拍。

→ **正确做法**:用 `CAP_RAW=1` 重建,抓 `{trace_b,trace_a}` 原始 nibble 字节(= LA 看到的同一份
引脚流),两路再走相同的 TPIU 去帧,才是 apples-to-apples。正在重建。

### 20.3 这也暴露一个设计问题

如果最终要 FPGA 实时出 ETM,traceIF 的片上锁帧逻辑也得和我们 PC 端验证过的 TPIU 去帧一致 ——
否则 traceIF 帧化的结果可能和正确去帧不符。先用 CAP_RAW 把"原始引脚→PC 去帧"这条和 LA 对齐,
再回头审 traceIF 的片上处理是否正确。


---

## 21. CAP_RAW 对拍:定位到 FPGA 数据 lane-1 的单沿采样问题

重建 `CAP_RAW=1`(抓原始 `{trace_b,trace_a}` nibble 字节 = LA 看到的同一份引脚流),重抓对拍。

### 21.1 字节直方图几乎一致,差异收敛到"一个 bit"

FPGA(nibswap 后)与 LA 的字节直方图高度吻合(`8c/05/90/01/88` 全对),**唯一系统差异在 bit1**:
- LA 有 `0x2e`(5030)、HSYNC `0xff`;FPGA 对应位置是 `0x2c`、`0xfd/0xdf/0xdd` —— 全部是 **bit1 被清 0**。
- HSYNC 段(本应全 1 = `0xff`)在 FPGA 里出现 `0xdd`(11011101,两个 nibble 的 bit1 都是 0)。

### 21.2 逐 bit set 率:lane-1 的**下降沿**采样异常

原始 FPGA 字节 `{trace_b[3:0],trace_a[3:0]}` 各 bit 的置 1 率:

| bit | 含义 | set 率 |
|-----|------|--------|
| a0 TD0↑ | 41% | bit1 a1 TD1↑ | **31.5%** |
| a2 TD2↑ | 19% | a3 TD3↑ | 47% |
| b0 TD0↓ | 34% | **b1 TD1↓** | **8.0% ←异常** |
| b2 TD2↓ | 65% | b3 TD3↓ | 49% |

TD1 的上升沿采样(a1=31.5%)基本正常,但 **TD1 的下降沿采样(b1)只有 8%** —— 明显偏低。
即 **数据 lane-1(XDC `trace_data_in[1]`=E14 ← STM32 PE4=TRACED1)在 DDR 下降沿的采样几乎丢失**。

### 21.3 性质判断

- LA(同一引脚)读 TD1 完全正常 → STM32 PE4 输出没问题,**故障在 FPGA 侧 lane-1**。
- 不是整条线死(上升沿还有 31.5%),而是**下降沿样本坏** → 指向该 lane 的 **IDELAY tap / IDDR
  Q2(falling)路径 / 该 lane 的建立保持窗口**,或 E14 这根的**接触不良/SI**(下降沿更敏感)。
- 这正是只有"上板对拍"才暴露的问题:之前 OOC 综合、单看锚点都发现不了。

### 21.4 下一步候选(按代价排序)

1. **先查物理连线**:E14 ↔ STM32 PE4 的杜邦线/接触(LA 的 D0~D2 之前也是接触问题)。换线重抓。
2. **per-lane IDELAY 校准**:lane-1 的 tap 单独扫(现在 4 lane 统一 tap=28);下降沿窗口偏了就单调它。
3. 若仍不行,审 `trace_capture_a7` 对该 lane 的 IDDR Q2 路径 / BUFR_IO 时钟相位。

**对拍工具已能把问题精确到"哪条 lane、哪个沿",这就是 ground-truth 对拍的价值。**

### 21.5 排除项:不是 IDDR 模式,也不是连线;是 lane-1 下降沿采样窗口

- **IDDR 模式排除**:把 IDDR 从 SAME_EDGE 改回 SAME_EDGE_PIPELINED(之前能抓到函数的配置)重建对拍,
  b1(TD1↓)仍只有 10.6%(对比 SAME_EDGE 的 8%),**两种模式都坏 → 不是我改 IDDR 引入的回归**。
  (IDDR 已保留为 SAME_EDGE_PIPELINED,即历史验证过的配置。)
- **连线排除**:LA 用同一引脚实测 TD1 = rising 32.3% / falling 30.7%,**两沿均衡正常**;FPGA 同一线
  = rising 31.4% / falling **10.6%**。STM32 PE4 输出没问题,故障纯在 FPGA 侧、且**只在该 lane 的
  下降沿(Q2)样本**。
- **性质**:上升沿好、下降沿坏、单 lane —— 是该 lane 的**采样窗口/ deskew** 问题(下降沿数据的
  setup/hold 关系和上升沿不同),不是死线、不是 IDDR 模式。
- **根因指向**:4 lane 共用静态 tap=28,lane-1 的下降沿眼图中心和这个 tap 不匹配。**需要 per-lane
  IDELAY 校准 / 眼图扫描**(本是 Stage-3 PoC 项,现在被对拍精确逼出来了)。

### 21.6 频率不是瓶颈(已澄清)

TRACECLK=1.316MHz(/64),数据率 ~10.5 Mbps;FPGA IDDR + 200MHz IDELAYCTRL + 125/200MHz MMCM
绰绰有余。问题是**采样相位(tap)**,不是跟不上速度。


---

## 22. 研究"边沿对齐源同步"的正确采样:根因 = 采在数据跳变点上

### 22.1 ARM 官方:TPIU 输出是**边沿对齐**的(决定性)

ARM CoreSight SoC TRM(TRACECLK alignment):**"The TPIU does NOT offset the edges of traceclk
from the edges of the trace data"** —— TPIU 不把时钟沿和数据沿错开,即**时钟跳变与数据跳变同时发生
(edge-aligned)**。ARM 建议**采集端(TPA = 我们的 FPGA)自己把采样点移到数据稳定区中心**
(CoreSight TRM:"create a sample point within the centre of the stable data ... on each changing
edge of TRACECLK")。

→ 即:**采集端必须移相,把采样点从"时钟边沿(=数据跳变点)"挪到"半位中心"。**

### 22.2 这就是我们 FPGA 下降沿坏的根因

- 我们的 IDDR **直接在 TRACECLK 边沿采** = 采在数据跳变点上。
- LA 之所以对:它 50MSa/s 过采,**我们在软件里取 edge+9 样本(180ns)= 半位中心**(`dsl_parse` 的
  `eye=half/2`)。这正是 ARM 说的"移到稳定区中心"。
- 实测对比(对 LA ground truth):FPGA 的 rising 值大致对、**falling 值系统性偏向 rising**,正是
  "falling 采样点踩在跳变沿、采到了过渡/上一拍数据"的表现。所有 lane 都受影响(TD1/TD2 最明显)。

### 22.3 orbtrace 为何不用移相?(ECP5 vs Xilinx 差异)

orbtrace testbench 注释明说"Cortex-M always sends LSB on L-H edge, so we don't need to worry about
phase" —— 但那是**理想 testbench**。真实 ECP5 上靠 `IDDRX1F` + `DELAYG` 的输入路径时序天然落在
眼内;orbtrace trace/ 里**没有任何 deskew/校准代码**,全靠 ECP5 原语特性 + TPIU 同步字锁帧。
**Xilinx IDDR 没有这个天然偏移,必须显式处理。**

### 22.4 1.3MHz 下可用的移相手段(关键约束)

需要把采样点移 ~190ns(四分之一周期 = 半位中心)。逐一排查:

| 手段 | 可行? |
|------|--------|
| IDELAY 延数据/时钟 | ❌ 最多 ~2.5ns,差 190ns 两个数量级 |
| MMCM/PLL 对 TRACECLK 移相 90° | ❌ TRACECLK 1.3MHz < MMCM 最低输入(~10MHz),锁不住 |
| **用相反时钟沿采(IDDR 反相/换边沿配对)** | ✅ **最可行** —— 在半位中心采,不需要绝对延时 |

核心思路:edge-aligned DDR 的正确采法是**在每个半位的中间采**,等价于"用 TRACECLK 反相后的边沿
去采 IDDR",或在 IDDR 里交换上升/下降沿与 nibble 的配对关系,使采样点落在数据稳定区中心而非跳变沿。

### 22.5 下一步(动手前再核一件事)

在改 RTL 前,先用 LA 数据做一次**离线验证**:把 LA 的采样点从 edge+9(眼中心)改成 **edge+0
(正好在边沿,模拟 FPGA 当前行为)**,看是不是也复现 FPGA 那种 falling 偏移、解不出锚点。
若复现,就 100% 坐实"采在跳变点"是根因,然后再改 FPGA 用反相沿采样。**先用零成本的 LA 仿真验证
假设,再动 Vivado。**

## 23. 落地:OVERSAMPLE 采集前端(复刻 LA 取样,提交 a441fc0)

### 23.1 为什么不是 §22.4 的"反相沿 IDDR"

§22.4 把"用反相 TRACECLK 沿采 IDDR"列为最可行。再想清楚后否决了它:

- edge-aligned DDR 的数据**每个半位都换一次**(上升沿一个 nibble、下降沿一个 nibble)。半位中心
  在"边沿之后约 1/4 周期"处,**不等于**对向边沿(对向边沿又是下一个数据跳变点)。
- 用反相沿采,只是把"踩在上升跳变"换成"踩在下降跳变",两个采样点都还在跳变沿上,没解决问题。
- 真正要做的是 LA 做的事:**在边沿之后等一小段(进入眼内)再 latch**。IDDR 给不了这个"边沿后延
  时 N"的语义,**过采(oversample)才能**。

### 23.2 OVERSAMPLE 模式(`trace_capture_a7.v`,`CAP_METHOD="OVERSAMPLE"` 默认)

完全复刻 `dsl_parse.sample_nibbles` 的 `s = edge + eye`:

1. 用 `ref_200m`(200MHz,5ns/拍)过采 TRACECLK(原始 IBUF)和 4 条数据线,各打两拍同步进 ref 域。
2. 边沿检测:`rise_evt = tck_s & ~tck_prev`,`fall_evt = ~tck_s & tck_prev`。
3. 每个边沿起一个 `EYE_DELAY` 拍倒计时;数到 0 时把同步后的数据 latch 进 `a_reg`(上升沿)/
   `b_reg`(下降沿)—— 即"边沿后 EYE_DELAY×5ns 落在半位中心"。
4. `a_reg/b_reg` hold 到下一个同向边沿(≈一个 TRACECLK 周期);traceIF 在 recovered `trace_clk`
   上读它们,看到稳定的 (a,b) 对。
5. `trace_a = a_reg`(上升沿 nibble,mid-eye),`trace_b = b_reg`(下降沿 nibble,mid-eye),与
   `trace_stream_top` 里 `nib = {trace_b, trace_a}` 的约定一致。

`EYE_DELAY` 默认 4(=20ns)。1.3MHz 时半位 ≈380ns,1..~70 拍都在眼内;取小值同时兼容更快的
TRACECLK。

低速 CDC 安全性:a/b 仅在边沿后 ~EYE_DELAY×5ns 更新、之后整周期不变,远离 trace_clk 的采样沿,故
低速下无需显式握手(高速 TRACECLK 才需要,超出"低速 100% 正确"里程碑范围)。

`CAP_METHOD="IDDR"` 分支保留原 IDDR 路径作参考/中心对齐源使用。

### 23.3 离线/仿真验证

- **LA 零成本仿真(§20-21 已做)**:LA 采样点 eye+0(边沿上)→ 锚点塌到 73~241;eye+1 起 → 满血
  818。坐实"采在边沿"是根因、"边沿后入眼"是解。
- **iverilog 功能仿真(本次新增)**:`rtl/sim/tb_oversample.v` + `xil_stubs.v` 驱动一个 edge-aligned
  源(数据在每个 TRACECLK 边沿翻转),DUT 必须 mid-eye 恢复出 trace_a/trace_b。结果 **99 checks /
  0 errors / PASS**。IDDR 分支也确认能 elaborate。

### 23.4 待办(综合后上板对拍)

综合中(后台,`CAP_RAW=1`)。完成后:program stream → etm_enable + downclock(/64)→ 重新 arm →
trace_dump → `fpga_la_crosscheck.py /tmp/fpga_raw.bin <194702.dsl>`。目标:FPGA 去帧后 unknown=0、
锚点集合与 LA 一致(MATCH)。

## 24. 决定性验证:把 .dsl 灌进 RTL 仿真(RTL 与 LA 字节级等价)

### 24.1 方法(用户提的关键思路)

与其在解码侧瞎试 parity/phase,不如**把 LA 的 .dsl 真实引脚波形当激励,逐样本灌进
`trace_capture_a7(OVERSAMPLE)` RTL 仿真**,跑同一条 `→ traceIF → CAP_RAW 字节` 链路,输出 RTL
重建的字节流,再用同一个解码器和 LA 软件路径对比。一刀切开:

- 仿真能解出锚点 = RTL 正确,板上分歧是真实硬件/SI;
- 仿真也解不出 = RTL 管线有 bug,且在仿真里逐拍可观测、零硬件成本。

工具:
- `decode/dsl_to_stim.py` — .dsl → 逐样本 memh(bit4=TRACECLK, bit3..0=TD3..TD0),50MSa/s。
- `rtl/sim/tb_dsl_replay.v` — 20ns/样本回放进 DUT,200MHz ref_200m 过采,在每个 recovered
  `trace_clk` posedge 抓 `{trace_b,trace_a}` 写出字节(与板上 CAP_RAW 完全一致)。

### 24.2 结果:逐位锁定根因

300k 样本回放,RTL-sim 重建的 nibble 序列与 LA 软件 `sample_nibbles` 逐个 diff:
**b-first(高半字节在前)、offset -2 时 15750/15750 nibble 完全相同(0 mismatch)。**

→ 即 RTL **采样完全正确**(每个 nibble 都对),唯一问题是字节里 nibble 的**配对顺序/相位**:
板上字节是 `{trace_b, trace_a}`(一个 trace_clk 周期的下降沿+上升沿),但 ETM 字节边界**不与
trace_clk 周期对齐**(parity=1:falling[k] 配 rising[k+1]),所以板上字节流不能直接解,必须把
nibble 拆回时间序后按 parity=1 / rise=low,fall=high 重新配对。

### 24.3 字节级等价证明(2M 样本)

同一 2,000,000 样本窗口,两条路径都跑"拆 nibble → parity 搜索 → TPIU 去帧 → 解码":

| 路径 | flash 锚点 | 去帧字节 | unknown |
|------|-----------|---------|---------|
| RTL-sim (tb_dsl_replay) | **33** | **34063** | **0.00%** |
| LA 软件 (dsl_parse) | **33** | **34063** | **0.00%** |

**完全一致。** `trace_capture_a7` OVERSAMPLE 前端经证明与逻辑分析仪金标准**字节级等价**。
硬件/SI 在仿真层面被排除,RTL 无 bug。

### 24.4 解码侧修复

`fpga_la_crosscheck.deframe_raw` 改为:byte → 时间序 nibble(b 先 a 后)→ parity/order 搜索 →
TPIU 去帧(与 dsl_parse 的 LA 路径一致)。板上 `/tmp/fpga_raw3.bin` 重解:0 锚点 → **18 flash
锚点**(loop_sum/add 的 0x08000f8c/f90/fa4/fb2/fba 全部正确),unknown 21% → 7.7%,与 LA 共有
7 个锚点。

### 24.5 残差归类(板上 vs 仿真的差)

板上 60KB 窗口仍有 7.7% unknown + 一个杂散 PC `0x08010f8c`(= 0x08000f8c 的 bit16 翻转),
**仿真里没有**。这正是被仿真隔离出来的**真实硬件残差**(单 bit 偶发错),与采样/配对逻辑无关
——下一步在板侧(更长窗口统计、IDELAY tap 微调、SI)收这部分,RTL 不用再动。

## 25. 仿真 100% 收口(全量验证 + 单元测试)

### 25.1 严格逐字节门禁(不只比锚点)

`decode/sim_la_equiv.py` 把"100% 正确"定义为最严:RTL-sim 去帧后的 ETM 字节流必须与 LA 软件路径
**逐字节相同**(长度 + 每个字节),而不只是锚点数/unknown 率相等。

多窗口回放(`tb_dsl_replay` OVERSAMPLE)对金标准 194702.dsl 的结果:

| 样本窗口 | RTL 字节 | LA 字节 | 失配 | 结论 |
|---------|---------|--------|------|------|
| 300k | 7876→… | = | 0 | 锚点等价 |
| 2,000,000 | 34063 | 34063 | **0** | BYTE-IDENTICAL |
| 10,000,000 | 170272 | 170272 | **0** | BYTE-IDENTICAL |
| **50,000,896(全量)** | **851453** | **851453** | **0** | **BYTE-IDENTICAL** |

全量回放的 RTL 输出再独立解码:**818 flash 锚点、unknown 15(0.0018%)、11 个 PC**,与 LA 金标准
逐项相同(那 15 个是 SysTick 异常信息字节,非真未知)。

→ **OVERSAMPLE 前端在仿真层面 100% 无错、与逻辑分析仪字节级完全等价,全量验证通过。**

### 25.2 对照:IDDR 模式(错误采法)

同一 2M 窗口、同一波形,`CAP_METHOD="IDDR"`(在 TRACECLK 边沿采)只得 **8** 个锚点,远低于
OVERSAMPLE 的 33——再次坐实"采在边沿"是错的、"边沿后入眼"是对的。

### 25.3 工具与单元测试

- `decode/dsl_to_stim.py` — .dsl → 逐样本 memh。
- `rtl/sim/tb_dsl_replay.v` — 回放进 DUT,EYE_DELAY 可经 `-P` 覆盖;`xil_stubs.v` 提供 iverilog
  用的 Xilinx 原语行为模型。
- `decode/sim_la_equiv.py` — 严格逐字节等价门禁。
- `decode/run_sim_equiv.sh` — 一键:.dsl → 激励 → 回放 → 逐字节校验(无需硬件)。
- `decode/test_capture_pipeline.py` — 10 个新单元测试:stim 位布局/往返、{trace_b,trace_a}
  时间序 nibble 提取、crosscheck 与 equiv 的 nibble 约定一致性、合成 TPIU 帧经"FPGA DDR 打包模型"
  往返恢复 I-sync 锚点、逐字节比较器、以及对真实 2M sim 产物的回归门禁。

测试总数:**144 passed**(原 134 + 新 10),含对真实仿真产物的等价回归。

### 25.4 下一步(上板)

仿真已钉死 100%。接着上板收**真实硬件残差**(板上 60KB 窗口曾见 7.7% unknown + 单 bit 翻转 PC,
仿真里没有):抓更长窗口看错误率是否随窗口收敛、扫 IDELAY tap 找最低误码点;真到 SI 瓶颈再上阻抗
匹配转接板。RTL 不需再动。

## 26. 上板残差诊断(用循环周期性定位)

### 26.1 思路(用户提出)

循环代码 → trace 周期性。即使板与 LA 是不同会话,也能:用周期自相关找循环周期、把板锁进 LA 共享窗口
逐字节走、按 bit 归因错误、折叠看固定位置还是随机。工具:`decode/period_align.py`
(periods/xcmp/rawfold)、`decode/onset_profile.py`、`decode/fpga_errrate.py`、`decode/lane_err.py`。

### 26.2 实测结论(都已验证)

- **EYE 位置不是主因**:EYE=4(20ns)与 EYE=38(半位正中)板上 unknown 都 ~8.5%,几乎一样。眼确实宽
  (sim 全程证明),不是采偏。
- **与时钟速度无关**:/64→/128(trace 速率减半)误码率不降反略升(7.7%→11.8%)。排除 setup/hold
  余量、亚稳态(这些随降速改善)。
- **读出链路无损**:同一冻结 buffer 连读两次字节完全相同 → 错误是采集时写进 BRAM 的,与 UDP/BRAM
  读出无关。
- **样本计数正确**:把板锁进 LA 后逐 nibble 走,允许 insert/delete 重同步——**0 个插入、0 个删除,
  全是 substitution**。即采集没丢/没多采样本,只是 nibble 值偶发错。
- **四条 lane 错误率大致相同**(TD0 19.6% / TD1 19.3% / TD2 12.9% / TD3 19.3%,本地锁定测量)——
  **没有单条坏线/虚焊**(若某根线 SI 差会单独高)。共模特征。
- **周期性结构完好**:raw 字节自相关锁定循环周期(~124),复现锚点 867 次,gap 集中在少数离散值
  (18/42/62/124)——采集的循环结构是完整的,不是乱漂。
- **错误时有时无**:最长干净 run ~500 nibble;deframe 后 unknown 按 decile 看会有整段 0% 整段 17%,
  且整段位置在不同 capture 间漂移(非固定 FSM/指针位置)。

### 26.3 当前判断(标注把握程度)

**确定**:RTL 逻辑正确(sim 字节级等价);板上是真实采集残差;非丢样本、非单线 SI、非时钟余量、
非读出损坏;四 lane 共模、~15% nibble 偶发值错、与速率无关。

**推断(待验证)**:最可能是采样点相对真实数据边沿的**相位/skew 共模问题**——data 经 IDELAY(16 tap
≈1.25ns)而 TRACECLK 不经,加上 ref_200m 2-FF 同步的 ±5ns 量化,使采样点偶发落在数据过渡带;LA 是
50MSa/s 真过采、软件取严格半位中心,板上 200MHz 过采的相位量化更粗。要验证需扫 EYE/IDELAY 的二维组合
或对齐 data 与 clock 的延迟链。

### 26.4 下一步实验(候选)

1. EYE 细扫(8/16/24/32/48/64)逐个烧录测 unknown,画误码-相位曲线,确认眼内最优点。
2. 给 TRACECLK 也加一条 IDELAY,与 data 对齐(消除 data-only 延迟造成的共模 skew)。
3. 提高过采率(若 ref 能到 300–400MHz)细化相位量化。
4. 若以上都压不到 ~0,再做阻抗匹配转接板(SI)。

## 27. 长 0 盲区 / 过采样方案的正确性核查 + CDC 修复(未命中)

### 27.1 长 0 盲区(Gemini 提的担忧)——不适用于我们

担忧:低频只能过采样;若算法从"数据边沿"恢复位时钟,遇到 ETM 长 0(A-sync/idle)数据不跳变会漂。
波形里确实每隔一段有一段全 0 数据。

实测核查(`decode/check_clk_in_zeros.py`,金标准 4M 样本):
- 最长全 0 数据段 = 229 样本 ≈ **4580 ns**(即波形里那段全 0,是 ETM A-sync/idle,正常)。
- **这段里 TRACECLK 照常翻转**:边沿数 11~12,与正常速率应有的 ~12 完全一致(381 ns/边沿)。

结论:我们的 OVERSAMPLE **在 TRACECLK 边沿采、每边沿产一拍**,用的是独立连续的源同步 TRACECLK,
**不从数据恢复时钟、不按固定分频计数**。长 0 段时钟还在翻 → 照常产 0000,不漂。且 sim 把含所有长 0
段的真实波形全量灌入,851453 字节 0 失配——长 0 真会漂的话 sim 早炸。故此坑不套在我们头上。
IDELAY 范围有限采不了低频,这点 Gemini 对,我们早已实测(§22.4),正是改用过采样的原因。

### 27.2 CDC 字节撕裂假说——已验证为否

假说:CAP_RAW 用异步 BUFR_IO `trace_clk` 写 ref 域产生的 `{trace_b,trace_a}`,跨时钟域字节撕裂。
修复:trace_capture_a7 在 ref_200m 域产出干净 `cap_byte`+`cap_valid`,top 用 `(clk200,cap_valid)`
写 BRAM,消除异步 CDC。sim 仍 100% 逐字节一致、144 测试全过。

板上实测:unknown 仍 **8.2%**,无改善 → **撕裂不是根因**(直接证伪,没靠假设)。

### 27.3 新线索:误码随"TRACECLK 与 ref_200m 的相位拍频"游走

多张 capture 的 decile 误码剖面:干净段位置在不同 capture 间**随机漂移**(eye38 开头净、raw3 结尾净、
cdc 中间净、eye8 全脏),不是固定起止瞬态、与数据活跃度无关、与 EYE 相位无关(4/8/16/38/64 全 ~8%)、
与速率无关(/64 vs /128)、读出无损、非丢样本。

这种"某段干净某段脏、位置随每次 re-arm 变"的特征,最像 **TRACECLK(异步)与 ref_200m 的相位拍频**:
相位有利时整段干净,拍频推进到不利相位时整段出错。指向异步采样在某些相位对齐下的系统性边沿/数据采错,
而非随机单 bit。待验证根因与对策(下一步)。

## 28. 决定性实验:板内同源异步回环(红队 r15 E1)——根因 = 物理 SI

### 28.1 实验设计

红队 r15 指出:之前"sim 全量 0 失配"是伪证据(xil_stubs 的 IDELAY 零延迟、时钟理想直通、TB 激励是
50MSa/s 离散样本对 5ns ref 的整数比栅格,结构上无法复现异步相位游走/skew/亚稳态)。要一刀切开
"(a) 异步过采架构缺陷" vs "(b) 物理 SI",需要一个**边沿干净、但时钟真异步**、且有 ground truth 的
板内实验。

实现(`SELFTEST=1`):在 **phy_rx_clk 域**(PHY 恢复时钟,与 MMCM 的 ref_200m 真异步,有真实拍频)
生成一个边沿干净的伪 TRACECLK + 4-bit 数据,每个边沿 **+7 mod 16**(coprime,16 边沿内不重复;丢/重采
变成 +14/+0 步,一眼可辨)。在 IBUF **之后**用 mux 注入 OVERSAMPLE 采样路径,**绕开物理引脚/IDELAY/SI**。
采集后离线校验"每 nibble 必须 +7"(内建 ground truth,无需跨会话对齐)。`decode/selftest_check.py`。

判据:有误码=架构在异步下就错(改架构);~0=架构对,板上 8% 是物理 SI。

### 28.2 结果(决定性)

SELFTEST 采集 122880 nibble:**99.903% 精确 +7,误码 0.097%**。对比真实 STM32 trace 的 **~8%**,
**差 ~80 倍**。

→ **异步过采架构在"干净边沿 + 真异步时钟"下是正确的**;板上 8% **主要是物理 SI**(杜邦线 + 面包板 +
共地的真实接线)。

### 28.3 连带揪出一个污染了所有历史测量的假象

那 0.097%(119 个错)**不是采样错**:它们成对出现在**精确每 2048 nibble = 1024 字节**处,边界字节是
**重复字节**(如 `...70 70...`)。1024 正好是 `trace_dump.py` 的 CHUNK=1024 UDP 分页读出边界——是
**BRAM 读出有 1 周期延迟、分页边界取到陈旧字节**的**读出 bug**,与采集无关。

**这解释了之前反复出现、被我误判为"ETM 同步周期 / 相位拍频"的 1024 周期性**(§26.2 的 4096/8192/16384
事件、§27.3 的"拍频")——其实一直是这个读出分页假象在污染测量。这是一条重要修正。

### 28.4 对红队 r15 裁决的诚实复盘

- **r15 对的(关键)**:① "sim 0 失配"是伪证据,结构上测不了异步/SI——完全正确,正是它逼出了 E1 这个
  真正有分辨力的实验;② "用四 lane 均匀排除 SI"是逻辑谬误;③ 测量方法学(跨会话模糊对齐)不可信——
  E1 的内建 ramp ground truth 正是对这条的修正;④ 硬伤 B(时钟链/数据链同步延迟差)真实存在但次要
  (体现为那少量 ±2 delta,实际还被读出假象盖过)。
- **r15 错的**:总裁决判 "(a) 异步采样架构缺陷是主因、(b) SI 物理上不合理"——**被 E1 证否**。架构在真异步下
  仅 0.097%;380ns 眼图下 SI 确实能造成 8%(低估了杜邦线/面包板/共地的真实劣化)。

### 28.5 下一步

1. **修读出 bug**:CAP_RAW 的 BRAM 读出 1 周期延迟在分页边界取陈旧字节 → 修 `ext_data`/`rrd` 时序或
   trace_dump 分页(让 ground truth 测量彻底干净)。
2. **SI 改善**(主线):做带阻抗匹配的转接板 / 缩短走线 / 加串阻 / 改善共地回流——这才是把板上 8% 压到 0
   的正路(与最初"先低速 100% 再提频,瓶颈就做阻抗板"的判断一致)。
3. 修完读出后,用 SELFTEST 复测应 → 0 误码,作为采集链(非物理层)的回归基线。

## 29. 对"8% = SI"结论的自我证伪:周期性 + 1MHz 还错说不通

用户质疑(对):物理 SI 若是经典眼图闭合类(反射/振铃/ISI),**不是周期性的**,且在 1MHz、半位 380ns
的巨大眼图下**根本不该出现**——眼心离边沿上百 ns,几 ns 量级的 SI 影响不到。我上一节"8%=SI"下得太快。

### 29.1 SELFTEST 没测到的东西

SELFTEST 用 FPGA 内部分频的**干净 50% 方波**当时钟,绕过了真实 STM32 TRACECLK 经杜邦线进来的边沿。
所以"SELFTEST 干净 vs 真实 8%"的差异里既有数据线 SI、也有真实时钟边沿质量,我之前一股脑算成数据线 SI。

### 29.2 真实 TRACECLK 边沿质量——LA 实测:完美

`decode/traceclk_quality.py`(金标准 4M 样本):TRACECLK 边沿间隔只有 18/19/20 样本
(360/380/400ns,即 ±1 样本的 50MSa/s 量化抖动),占空比精确 50.0%,**0 个毛刺/异常短间隔**(无振铃/慢沿
穿越阈值)。→ "时钟边沿质量差 → 边沿误计数"假说**也被否决**。TRACECLK 在 LA 看来干干净净。

### 29.3 矛盾 → 真正的嫌疑转向"测量方法本身"

事实摆在一起互相矛盾:真实时钟边沿干净 + SELFTEST 架构 ~0% + 真实 trace 8% + 那 8% 的"周期性"已证明是
**读出 bug 假象**(§28.3,1024 边界)。这逼我重审 8% 怎么来的:我一直用"去帧后 unknown 率"当指标,而
**跨会话、无 FSYNC、单一全局 TPIU 相位 + parity 搜索**的离线解码流程本身可能在真实长流上引入错误。
即:**8% 里可能有相当一部分不是采集错,而是离线去帧/对齐流程的缺陷**(正中红队 r15 S5:测量方法学不可信)。

### 29.4 下一个判定实验:SELFTEST 走真实物理引脚回环

在"采集链已证对"(§28)基础上,把 SELFTEST 的 ramp 从 **FPGA 输出脚发出 → 杜邦线物理回环 → trace 输入脚**
采回。好处:① ramp ground truth 仍在,**不需对拍、不需去帧**,指标干净;② 信号经过真实物理链路。
- 若回环后误码骤升 → 真的是物理 SI(且能定量)。
- 若回环后仍 ~0% → 物理链路没问题,之前的 8% 主要是**离线去帧/测量流程**的锅,不是采集也不是 SI。
这一步把"采集逻辑 / 物理 SI / 离线测量流程"三者彻底分开。

## 30. 真根因:读出分页 off-by-one —— 8% 几乎全是测量假象

### 30.1 用 SELFTEST ramp 精确刻画读出 bug

`req(base,n)` 实测:每个 UDP 请求的**第一个数据字节总是重复 source[base]**(陈旧读):
- `req(1024,8)` = `70 70 5e 3c...`、`req(1023,8)` = `92 92 70...`、`req(1022,8)` = `b4 b4 92...`。

根因:CAP_RAW 的 BRAM 读 `rrd` 有 **1 周期延迟**(`always @(posedge clk125) rrd <= rawmem[ext_addr]`),
但 `ext_data` 组合用当前 `ext_addr` 选 `rrd`,于是每个请求的首字节滞后一拍 = 重复 base 字节。trace_dump
按 CHUNK 分页,**每页接缝多塞一个重复字节**,后续整体错位 → 这就是 §28.3 的 `70 70`、§26/§27 反复出现
被误判成"ETM 同步周期 / 相位拍频"的 1024 周期性。**它污染了所有用 trace_dump 抓的数据。**

### 30.2 修正 + 验证(决定性)

修法:trace_dump 每页请求 `n+1` 字节、丢弃首个重复字节(`scripts/trace_dump.py` 已固化)。

- **SELFTEST ramp**:修正后 122878 nibble **一条连续干净 ramp**,误码 0.002%(原 0.097% 全是读出假象)。
- **真实 STM32 trace**:修正后 unknown **8% → 0.003%(1 字节)**,flash 锚点 20 → **39**,**0 个杂散 bit-flip PC**。
  两次独立 re-arm 复测:0.003% / 0.003%,稳定可复现。
- **与 LA 金标准对拍**:FPGA unknown 0.0025% vs LA 0.0018%,**FPGA-only PC = 0**,8 个共有 PC 全中
  (LA 多 3 个是因为它窗口长 818 锚点;FPGA 60KB 窗口没覆盖到,非错误)。**质量上已与逻辑分析仪持平。**

### 30.3 复盘:三方都判错了根因,ramp ground truth 一照现形

- 我先判"采样相位/CDC 撕裂",再判"物理 SI";红队 r15 判"异步采样架构缺陷"。**全错。**
- 真凶是一个朴素的**读出寻址 off-by-one**,与采样、CDC、SI、相位**全无关**。
- 能定位它,靠的正是红队逼出来的 **SELFTEST + ramp 内建 ground truth**(不依赖跨会话对拍/去帧)——
  r15 的方法学批评(S5:测量不可信)是真正有价值的一刀,尽管它自己的根因裁决也错了。
- **教训**:在"测量管道本身"未被独立验证前,任何基于它的根因推断都不可信。应当**先用已知 ground truth
  把测量链(采集 RTL + 读出 + 离线解码)标定干净,再去测未知信号**。

### 30.4 现状 = 低速 100% 正确里程碑达成

低速(/64,~1.3MHz)下,FPGA 采集 + 读出 + 解码全链路与逻辑分析仪金标准质量持平(unknown ~0.003%,
锚点全中,无杂散)。采样架构(§28 SELFTEST)、读出(§30 修正)、解码(§24 sim 字节级等价)三段都已各自
用 ground truth 独立验证。**可以开始提频压承压上限了**(SELFTEST 也可作为提频时分离"采集链 vs 物理层"
的常驻探针)。

### 30.5 遗留(次要)
- RTL 侧也应修 `rrd` 读延迟对齐(目前在 trace_dump 侧规避;RTL 修了更干净,且让 ext_data 语义正确)。
- 残留 ~0.003% 的个位数字节:可能是真实异步采集的极偶发,或 ETM 异常信息字节,提频时再观察。
