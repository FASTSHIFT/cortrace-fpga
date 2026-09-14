# r29 — 审核 `r28-response-ETMv4过滤与假调用机理.md`（原文逐行核验）

**日期**：2026-07-19
**对象**：`reviews/r28-response-ETMv4过滤与假调用机理.md`（C1–C8）
**核验手册**：`refs/ihi0064h.txt`（IHI0064H.b，56262 行，本次逐行比对）
**立场**：严格证伪；无法成立的质疑也诚实标注。

---

## 一句话裁决

> **C1–C5、C8 引用属实、结论成立，原文核验全部对得上——这是本项目迄今引用纪律最好的一份文档，值得肯定。但全文两大支柱之一的 C6/C7 被误用：手册自己的 Table A-14（返回栈 disabled 时的 trace 示例，line 44057）白纸黑字写明——RS=0 时间接分支/返回 _会_ 发 Address element（是锚点）。蓝方当前配置正是 RS=0（C8），所以 C6"返回不发 Address、返回栈补错自我污染"这条机制在本配置下不成立；C7 把它标成"待验证【推断】"更是搪塞——手册在 Appendix A 已经给了确定答案，蓝方没去查自己引用的手册的示例章节。** 结论：假调用若真为 ETM 机理，则**纯是 C4 直接分支盲推越界**，返回栈（C6/C7）不参与；而"固有局限、非解码 bug/非 ELF 不一致"这个甩锅式定性**未被隔离验证**，Q4 的更简单证伪（ELF≠固件 / 解码器 call 重建 bug）没有被排除。

---

## 一、C1–C8 逐条判定（附行号核验）

### C1 — ViewInst 只按地址过滤，无指令类型维度　✅ **成立**
核验 `ihi0064h.txt` §4.1.3（line 9928-9960）：原文 "instruction tracing can be made active or inactive **based on instruction addresses**"，随后列举 Start/Stop、include/exclude address ranges、enabling event、Exception level——**全部是地址/事件维度，无一条按指令类型**。引用属实，改述准确，结论成立。补充的 `NUMACPAIRS=0`（C8 读回 TRCIDR4=0x00114000，bit[3:0]=0）也与"连地址区间过滤都不可用"自洽。

### C2 — BB 只控制直接分支的地址广播　✅ **成立**（章节号有小误）
核验 line 6871-6873 与 line 7117-7119：两处原文均为 "explicitly traces the target addresses of **direct branch and ISB instructions**"。作用域确为直接分支+ISB。**瑕疵**：文档标注 "§2.4.4"，实际原文在 **§2.7.5 Branch broadcasting**（line 6864）。章节号错，但**引文与结论正确**，不影响成立。

### C3 — 调用=直接分支、返回=间接分支（Appendix F）　✅ **成立**
核验 Appendix F：Table F-5（T32 32-bit direct branches, line 46860）含 BL/BLX immed；Table F-6（indirect, line 47008）与 Table F-8（16-bit indirect, line 47165）含 BX / POP including the PC。分类属实，结论成立。

### C4 — BB-OFF 下直接分支目标靠解码器从 ELF 盲推　✅ **成立**
核验 §5.2（line 16945-16948）：原文 "For **direct branch** and ISB instructions, a trace analyzer must **infer the target address** … from the instruction opcode in the program image."——逐字吻合。"盲推"的架构定义准确，结论成立。**这是假调用成因链里唯一被原文硬支撑的机制。**

### C5 — 间接分支原则上发 Address 元素（锚点）　✅ **成立**
核验 line 16179-16182：Address 元素 "is generated when an **indirect branch** is taken or when an exception occurs or after a Q element is generated."——吻合。结论成立。

### C6 — 返回栈例外：返回不发 Address、解码器 pop 返回栈　⚠️→🟥 **原文属实，但对当前配置误用（证伪其适用性）**
- **引文属实**：line 19614-19623（§5.3.4 trace analyzer return stack）原文吻合。
- **但这段描述的前提是"trace unit 不发 Address element"**，而这只在 **trace-unit 侧返回栈使能（TRCCONFIGR.RS=1）** 时发生。核验 §5.3.1（line 19456-19488）："FEAT_ETMv4 includes an **optional** return stack … removes address elements … TRCIDR0.RETSTACK indicates if the return stack is implemented"，且地址省略只在"indirect branch … matches the top entry"时发生。
- **决定性反证**：手册 Appendix A 的两张对照表——**Table A-14 "return stack disabled"（line 44057）** 明确列出：`BX LR` 返回后在 0x1004 处 **"the trace unit generates an Address element (0x1004)"**；**Table A-15 "return stack enabled"（line 44182）** 才是省地址、走返回栈。
- **C8 实测本机 RS=0（TRCCONFIGR=0x9，bit12=0）**。⇒ 按 Table A-14，本配置下**返回照发 Address element，是可靠外部锚点**。C6 描述的"返回不发 Address、返回栈自我污染"**在当前 RS=0 配置下不发生**。
- **判定**：C6 作为 RS=1 的一般性陈述成立；作为**当前假调用成因**被误用——**证伪其在本配置的适用性**。

### C7 — 解码器无条件维护 15 深返回栈；返回不构成可靠锚点　🟥 **前半成立、结论证伪、【推断】被过度使用搪塞**
- **前半属实**：line 19611-19612 "the trace analyzer **must implement a return stack with a depth of 15 entries**"——解码器确实必须实现返回栈。
- **但"⇒ 返回不构成可靠外部锚点"是非逻辑跳跃（non-sequitur）**：解码器"实现"返回栈 ≠ 会"使用"它。手册 line 19621-19623 明确：只有当"trace unit … **does not output an Address element**"时才 pop 返回栈。RS=0 时 trace unit **会**发 Address（Table A-14），所以解码器的返回栈**根本用不上**——手册 line 19532 亲口说 "In the trace analyzer, those return stack entries that are retained are **never used**."
- **【推断】被过度使用**：C7 标注"ETM 侧 RS=0 时返回到底发不发 Address 元素**未验证**，只能 packet-level 验证"。**这是搪塞**——蓝方引用的同一本手册，在自己没读到的 Appendix A（§A.6, Table A-14）里已经给了确定答案：**RS 关闭 → 返回发 Address element**。把一个手册已明确回答的问题标成"待验证推断"，是把知识空白伪装成客观不确定性。
- **判定**：前半成立；核心结论"返回不构成可靠锚点"在 RS=0 下**证伪**；【推断】使用不诚实（可查而未查）。

### C8 — 本 M7 硬件读回　✅ **成立**（数值自洽）
核验位运算：TRCIDR0=0x080006e1 → bit9=(0x6e1>>9)&1=1（RETSTACK 实现）；TRCCONFIGR=0x9 → bit0=1、bit3=1、bit12=0（RS=0，返回栈未开）；TRCIDR4=0x00114000 → bit[3:0]=0（NUMACPAIRS=0）；RS 位语义引用 line 35755-35762 属实。**瑕疵（已被文档自己标注）**："bit3 BB=1(读时)"与阶段 2/3 的 BB-OFF 运行矛盾——这是 openocd 读时复位的已知伪影，非载荷，可接受。数值判定成立。

---

## 二、总结论裁决

文档总结论：**"假调用 = ETM 压缩模型（C4 直接分支盲推 + C6/C7 返回不锚定）× cache 稀疏锚点的固有解码局限，非采集丢字节"**。

**裁决：部分成立、部分证伪、整体定性未隔离。**

1. **C4 部分成立**：BB-OFF 下直接分支靠 ELF 盲推、cache 拉大锚点间距 → 盲推越界，机制真实、原文支撑（C4）、与 R1（采集非因）自洽。
2. **C6/C7 部分证伪**：返回栈"自我污染"在 RS=0 下不发生（Table A-14：返回发 Address = 锚点）。蓝方把一个 **RS=1 才成立的机制**混进了 RS=0 配置的成因链。**这恰恰是本项目老毛病的又一例：把"部分条件成立"（RS=1）写成普适。**
3. **更关键：出现逻辑悖论，反证 C6/C7 不是主因**。若 RS=0 下返回确为锚点（手册所述），则 `core_list_init→HAL_UART_Init ×22` **持续 22 次不被纠回**就无法用"返回栈补错"解释——每次返回都应发 Address 把 PC 拉回。22 次持续错，要么是 (i) C4 盲推每轮迭代产一次错、返回锚点其实在别处、fake call 落在两锚点之间；要么 (ii) **根本不是 ETM 机理，而是解码器 call-graph 重建 或 ELF≠固件**（见 Q4）。文档没有触及这个悖论。

**⇒ "固有局限、非采集/非 bug" 这个高级听感的定性，只隔离了"采集丢字节"（R1 已双证），但_没有_隔离"解码器 bug"和"ELF≠固件"——而后两者会产生与"盲推越界"完全相同的 cache 依赖签名。**

---

## 三、红方五问逐条回答

**Q1（引用核验）**：C1/C2/C4/C5/C8 引用属实、无断章取义（C2 章节号笔误不影响）。**C6 引用属实但适用条件被隐去**（只在 RS=1 成立，文档未标"仅 RS=1"）。**C7 前半属实、结论超出原文**。支柱之一 C6 因此塌一半：C1（无类型过滤）稳固，C6（返回栈补错）在本配置不成立。

**Q2（C7 推断是否搪塞、是否动摇因果）**：**是搪塞，且动摇因果。** 手册 Table A-14（line 44057）已确定 RS=0 时返回发 Address element = 锚点。所以"如果 RS=0 返回仍发 Address，则假调用纯是 C4 盲推越界、与返回栈无关"——**这个红方假设被手册证实为真**。蓝方把一个未证机制（返回栈补错，实为 RS=1 行为）混进了成因，而当前恰是 RS=0。C7 的【推断】不该存在——答案在它引用的同一本手册里。

**Q3（逻辑链是否跳步/循环）**：C1+C2+C3+C4 链条自洽；**C6 是跳步**（RS=1 机制套到 RS=0）。与 R1 的独立证据（BB-OFF no-cache 干净、BB=1+cache 干净）**部分自洽**：这两个对照确实排除了"采集丢字节"（BB=1+cache 干净说明前端/deframe/trace-ID 在密锚点下正确）。**但不排除 ELF≠固件与解码器盲推 bug**（见 Q4）——所以不是纯循环论证，但隔离不彻底。

**Q4（更简单的证伪：解码器 bug / ELF≠固件 / deframe 错位）**：**这是本文档最大盲区。**
- **BB=1+cache 干净**能排除 deframe/trace-ID 错位（那会同时污染 BB=1）和采集丢字节。**但不能排除 ELF≠固件**：BB=1 给每个直接分支显式 Address，解码器**无需盲推**，所以 ELF 若与固件有出入，在 BB=1 下被显式地址掩盖、在 BB-OFF 下才暴露——**ELF 不一致的签名与"盲推越界"完全相同**（都是 BB-OFF 脏、BB=1 干净）。
- **BB-OFF no-cache 干净**削弱但不排除 ELF 不一致（低锚点间距下盲推走不到不一致区）。
- 阶段 0 的 CoreMark CRC 全对**只证明固件计算正确**，**不证明交给 OpenCSD 的 ELF/.axf 与 flash 里的镜像逐字节一致**（可能重编译、flag 不同、地址错配）。文档全程没有 ELF↔flash 的同一性校验。
- **结论**：把假调用归给"ETM 压缩模型固有局限"在**未先排除 ELF 不一致和 OpenCSD call 重建 bug** 的情况下，有甩锅（把可查的工程 bug 归给听起来高级的架构限制）之嫌。**判定：Q4 的替代解释未被排除，定性不能坐实。**

**Q5（诚实性审查）**：**喜忧参半，但整体比 proposal 34 诚实。**
- **进步**：C1–C5/C8 逐条附行号原文、【推断】显式标注、承认"只抓 BL/BLX/POP 硬件不可达"——引用纪律是本项目最好的一次。
- **旧毛病复发**：(i) C6 把 RS=1 机制当普适（"部分条件成立写成普适"）；(ii) C7 用【推断】搪塞一个手册已答的问题（可查未查）；(iii) 总结论"固有局限"在未排除 ELF/解码 bug 前下定性（甩锅式解释）。

---

## 四、下一步唯一最该做的验证实验

**我不同意"C7 packet-level 日志"是最高优先级。** 理由：C7 想验证的"RS=0 返回发不发 Address"，手册 Table A-14 已经回答（发）。先做两个更根因、更便宜的：

### 实验 1（最高优先级，零上板，10 分钟）— ELF↔flash 同一性校验
把交给 OpenCSD 的 `.axf`/ELF 的 `.text` 段与从 H743 flash 读回的镜像（openocd `dump_image`）做**逐字节 diff / CRC**。
- **判据**：不一致 → 假调用根因是 **ELF≠固件**（工程 bug），`opencsd_etm4_run` 换用与 flash 一致的 ELF 重解，假调用应消失。**在此之前任何"ETM 固有局限"的定性都不成立。**
- 这一步直接证伪或坐实 Q4 的最大替代假设，成本几乎为零。

### 实验 2（次优先，零上板）— 假调用目标是"盲推 PC"还是"流中真 Address"
对同一份 BB-OFF+cache 字节流开 `trc_pkt_lister -decode` packet-level 日志，定位 `HAL_UART_Init` 假调用区间，看该目标地址是：
- (a) 由一串 **atom + 解码器盲推 PC** 到达（无 `I_ADDR_*` 包指向它）→ 坐实 **C4 纯盲推越界**（返回栈无关，C6/C7 从成因链剔除）；
- (b) 伴随一个 **真实 `I_ADDR_*` 包**指向 `HAL_UART_Init` → 那这个地址**真的在 ETM 流里**，问题回到"这个地址是真执行的（则不是假调用，是 Perfetto 栈重建 bug）还是被采集/解码破坏的地址字节"——**此时必须回到采集完整性**（推翻 R1 的"非采集"结论）。

**注意**：实验 2 就是蓝方 C7 提的 packet-level 日志，但**问题被重新定义**——不是问"RS=0 发不发 Address"（已知发），而是问"假调用目标是盲推 PC 还是流中真 Address"。且必须在实验 1（ELF 同一性）通过后做，否则实验 2 的"盲推越界"会被 ELF 不一致污染。

### 顺序与判据总表
| 步 | 做什么 | 判据 | 结论分叉 |
|---|--------|------|---------|
| 1 | ELF↔flash 逐字节 CRC | 一致? | 不一致→根因=ELF不符（非架构），换ELF重解，收工 |
| 2 | packet-level：假调用目标来源 | 盲推PC / 真Address? | 盲推→C4坐实、C6/C7剔除；真Address→回采集完整性(推翻R1) |

**在实验 1 未过之前，不要采信"假调用=ETM 压缩模型固有局限"这个定性，也不要据此上 range-filter/加密 A-sync 的替代方案（况且 C8 已证这些硬件不可用）。**

---

## 结论表：C1–C8 判定

| # | 结论 | 判定 | 关键行号/理由 |
|---|------|------|--------------|
| C1 | ViewInst 无类型过滤 | ✅成立 | line 9928-9960 逐字吻合 |
| C2 | BB 只管直接分支 | ✅成立 | line 6871/7117 吻合（章节号笔误§2.7.5） |
| C3 | 调用=直接、返回=间接 | ✅成立 | Table F-5/F-6/F-8 |
| C4 | BB-OFF 直接分支靠 ELF 盲推 | ✅成立 | line 16945-16948 逐字吻合 |
| C5 | 间接分支发 Address（锚点） | ✅成立 | line 16179-16182 吻合 |
| C6 | 返回栈使返回不发 Address | 🟥成立但**误用**于 RS=0 | line 19614+ 属实；但仅 RS=1；Table A-14 反证 |
| C7 | 解码器无条件维护返回栈⇒返回非锚点 | 🟥前半成立、**结论证伪**、推断搪塞 | line 19611 属实；line 19532/A-14 反证结论 |
| C8 | 硬件读回 RS=0/RETSTACK=1/NUMACPAIRS=0 | ✅成立 | 位运算自洽；line 35755 属实 |

**总结论**：C4 盲推成立；C6/C7 返回栈补错在 RS=0 配置**不成立**；"固有局限非 bug"定性**未隔离 ELF≠固件与解码器 bug**，不能坐实。

---

## 一句话给用户

**这份文档的手册引用是本项目最扎实的一次，C1–C5/C8 逐行核对全对，值得表扬；但两根支柱之一塌了——C6/C7 讲的"返回不发地址、返回栈补错自我污染"是返回栈 _开启_（RS=1）才有的行为，而你们实测 RS=0，手册自己的 Table A-14 白纸黑字写明 RS=0 时返回 _会_ 发 Address element（是锚点），C7 还把这个手册已经回答的问题标成"待验证推断"来搪塞。所以假调用若真是 ETM 机理，就纯是 C4 直接分支盲推越界，返回栈根本不参与；而"这是 ETM 压缩模型固有局限、不是 bug"这个定性，只排除了"采集丢字节"，没排除"ELF 和 flash 里的固件不一致"和"OpenCSD call 重建 bug"——这两个会产生一模一样的"BB-OFF 脏、BB=1 干净"签名。下一步别急着开 packet-level 日志，先花十分钟把交给 OpenCSD 的 ELF 和 flash 读回镜像做个 CRC 比对——ELF 不一致是最简单、最可能、且从没被验证过的甩锅出口。**
