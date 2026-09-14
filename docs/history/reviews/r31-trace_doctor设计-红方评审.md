# r31 — 审核 `proposals/40-一键诊断工具trace_doctor设计.md`（红方）

**日期**：2026-07-25
**对象**：proposal 40（trace_doctor 一键诊断工具设计）
**复核**：`AGENT.md`、`rtl/dbg_regfile.v`、`rtl/trace_stream_top.v`、`rtl/trace_mmcm_stream_top.v`、`scripts/hw_selftest.py`、`docs/artix7-port/proposals/{22,25,30,32,33,38,39}*.md`
**立场**：严格证伪；无法成立的质疑也诚实标注。

---

## 一句话裁决

> **设计方向对、动机真实，且 §9 补充引入 dbg_regfile 是本项目最有价值的一次修补——但整个设计的可靠性建立在两块砖上，其中一块（dbg_regfile 的边沿计数器/频率计）在目标频段（TRACECLK 100M+）里几乎肯定漏计，工具会自信地报错误的频率并据此误判 PASS/FAIL。** 具体：`gpio_clk_edge = gclk_s1 ^ gclk_s2`（clk125 采样的两拍 XOR）在 100M TRACECLK 下**是欠采样**（Nyquist 55.6M，远低于 100M），会稳定性地少数 20-50% 的边沿——`TRACECLK_FREQ` 报 50-80M 是虚假读数、`GPIO_EDGES` 值不能作为"引脚是否翻转"的真值。§7 的 `TRACECLK 目标±5%` 判据在这条链上会**PASS 假故障、FAIL 真正常**。除此之外的其它质疑多为小刀（阈值需实测校准、少数漏坑、方案 A 的时序风险蓝方低估），但 dbg_regfile 频率计这一条足以否定"先做 P0 兜底、并行做 P0.5 重综合 A"的路径——**必须先修 RTL 的边沿检测机制**（改到 clktap MMCM 时钟域，或改用触发器分频/异步计数），否则 §9 强化的 L4 层是自信的错。**

---

## §1-9 逐节判定

### §1 覆盖完整性（35+ 失效模式）　🟨 **存疑：仍有漏项，且是今天真的踩到的**
清单穷举度确实高（35 条覆盖了主机/物理/bit/网络/相位/debug/时钟/固件/解码八层），比之前的排查风格上了一个台阶。但对着 AGENT.md 和最近两天真实卡壳过程逐项核对，仍有下面这些**已被踩过或结构上就存在**的失效模式没进清单：

- **CoreMark 一轮 >3s 期间被 halt 打断 → 永远打不出 banner**：文档 §3.3 提到"不要 reset halt"，但没有列为独立失效模式，也没有对应的判据（比如"halt 到 banner 间隔阈值"）。这是今天真的卡过的。
- **openocd 与 openFPGALoader 抢占顺序**：AGENT.md 有相关坑，清单只列了"openocd 残留"和"openFPGALoader 报 Done 但没换 bit"，**没列它们同时跑相互干扰**（USB 总线抢占、DAPLink 出 I/O error）。
- **STM32 halt 期间 UART TX 缓冲丢失 / DMA 卡死**：halt 会冻结外设时钟依赖，resume 后 DMA/UART 可能不恢复；proposal 37 讨论过 SysTick 相位与 wall-clock 关系，但 UART 侧的失效模式没进清单。
- **make 判"无需重编" + hex 与串口 banner FLAGS_STR 版本对不上**：AGENT.md §4 明确列了这条坑（"改宏后先 `make clean`"），清单 §31 把它写成"固件 make 判无需重编"，但**没有配套判据**（L6 层的"UART banner FLAGS_STR = rt-cache300M"是文本正则，但没规定"如果 banner 缺失怎么处理"，正好会命中"halt 打断串口"这条）。
- **`.trace_doctor.bit_db.json` 白名单本身漂移**：项目里 bit 文件按需重综合，md5 会变，白名单要人肉维护，否则合法新 bit 会被判 FAIL。
- **VMware USB 独占**：§4.3 提到"设备在 host 侧被占用"，但缺具体判据（`lsusb -v` 或 `dmesg | grep -i usb` 检测）。
- **主机 sudo 会话过期**：AGENT.md 说密码 `asd`，但 sudo 缓存到期后 `-n` 会失败——§4.3 有一句"测一次 sudo -n"，但没列成独立失效模式。
- **hgfs 掉线时 make 拿到空目录**：AGENT.md 提到 hgfs 会掉，但清单 §4 只写"hgfs 掉线：/mnt/hgfs 变空"，没列**hgfs 半可用**（能 `ls` 但 `read` 挂）的中间态。

**判定**：清单方向对、比之前进步显著，但**没到"穷举"**——至少 3-5 条今天真踩过的、AGENT.md 明确记录过的失效模式缺席或缺配套判据。**这条虽不阻断，但暴露了"设计文档时靠回忆列坑"vs"对着 AGENT.md/git log 系统性核对"的方法差异。**

### §2 层间依赖 + 早停　🟨 **存疑：早停模型对本项目而言可能过强**
"每一层是下一层的前提，任一层 FAIL 立即停止" 是软件测试"shift-left"理念的直搬。**它假设失效模式的因果都是自上而下的**——但本项目实际观察到的一些失效偏偏是**"上层看着 PASS、下层 FAIL 后反查发现上层的隐藏位也坏"**：

- **proposal 38 教训**：mem 基址 0x2a0 skew 被当作"ETM 架构问题"分析了好几轮——**上层"ELF 已提供、opencsd 已加载"这一步 PASS（没红），下层"解出 0 PC"FAIL 才反查出来 mem 基址错**。按 §2 早停，如果 L7 ETM 数据流 PASS，L8 opencsd 解出 PC=0，早停就停在 L8——但根因其实在 L6 "ELF↔flash 一致性" 或 L8 内部的 mem 基址计算。清单里 §L6 有 "ELF vs flash md5"，但**没有 "mem 基址与 ELF 段基址一致性"**（这正是 proposal 38 的真凶），早停会掩盖它。
- **TRCAUTHSTATUS 位状态**：清单 L5 判 non-invasive=11 为 PASS。但今天实测里"L4 TPIU voltmeter PASS + L7 ETM 数据出不来"确实发生过——这时候需要**下游反查上游**（回头看是不是 secure/non-secure 授权某位漏了、或 DBGMCU_CR 某位其实需要非默认值）。早停一旦停在 L7，就丢掉了"回头改测 L5 深度位"的机会。
- **L4 相位 PASS + L5 debug PASS + L7 ETM 数据流看似 PASS + L8 opencsd 全走盲**：这类"逐层绿、结果错"的失效在 r28/r29/r30 一路批过（PASS 判据本身不够严格）。早停停在最后一步都 PASS，但整链其实错了。

**判定**：早停在**普通失效模式**下加速定位有效；但对**层间隐藏耦合**（proposal 38 类型的 bug）会掩盖真凶。建议 §2 增加一个"全绿但用户主诉仍失败 → 强制走 `--continue-on-fail` 全跑 + 深度模式（L6-L8 加严判据 + 交叉验证）"的分支，而不是把"逐层绿即整体绿"当保证。**这是本项目老毛病（r25/r28/r30 一路批的"逐层单变量绿、组合失败"）在诊断工具设计里的隐性复发。**

### §3 CLI + 组件 + openocd 会话复用　✅ **成立**
CLI 设计合理，`.bit_db.json`/`.fw_expect.json` 白名单方向对，`halt→读→resume` 而非 `reset halt` 是本项目血泪教训的正确应用。P0.5-P2 的复用现有工具列表（hw_selftest / trace_ctrl / opencsd_etm4_run / verify_calls）齐全。此节唯一小刀：**白名单文件的版本化和 CI 更新流程没定**——bit 重综合后谁负责更新 `.bit_db.json`？agent 自动？人肉？漂移到手一天就废。

### §4 补漏（温度/版本/权限/兼容 + 交叉验证 + 兜底）　✅ **成立且是加分项**
温度（XADC）、arm-none-eabi-gcc/opencsd 版本、"同 workload 重复 3 次 CoreMark 方差 <0.5%"、`--dump-all` bundle——这些是**红方式补漏**的正确操作，比第一版单纯堆判据的路线更成熟。**这一节做得最好**。**但**：§4.5 的"3 次抓样 fsync 方差 <20%"这个 20% 阈值哪里来？未标出实测依据；同样 §4.5 的"CoreMark 方差 <0.5%" 依据何来？两个阈值都需要**先跑一遍基线**（无故障态）实测方差分布再定，否则 §7 里那些"±5%"/"20%"数字全是拍脑袋（见 §7 判定）。

### §5 分阶段落地　🟨 **存疑：P0 一天低估了健壮性成本**
- P0（一天）**低估** openocd 会话流的时序坑：AGENT.md 已经明确 openocd 卡在 halt+resume+shutdown 时序是常客，`halt→读→resume` 看似简单，实际今天多次卡在这里。**P0 至少 1.5-2 天**才能拿到"能真正拒绝所有已知失效模式"的可靠 shell 驱动。
- P0 依赖 hw_selftest 的 pin_la bit，**但 pin_la bit 和 clktap bit 是两个 bit**，切换 bit 本身要几秒 + 需要 openFPGALoader；如果 L2 一开始就发现"当前 bit 不是 pin_la"，L4 走不通——**L4 相位测试的"烧 pin_la bit"是隐藏前置**，不是"读一个 CSR"就能做的诊断。文档 §2 没澄清 L4 是否要重烧 bit。
- P2（一天）自动修复：`--fix tap_scan` 需要 30-60 秒（tap 0-31 每档抓样 + 评分），加上 openocd 交互，实际时长可能远超 30 秒 SLA。
- **"30 秒定位失效层"这个 SLA 没有实测依据**——只是宣传口号，文档没算过 L0-L8 每层实际耗时之和。粗估：L0 0.5s + L1 1s + L2 0.5s + L3 1s + L4 5-10s（要烧 pin_la 或读边沿）+ L5 2s + L6 2-5s（读 UART banner） + L7 1s + L8 5-15s（抓+解） ≈ **20-40 秒**，30 秒在**顺跑绿**时刚够，一旦有 FAIL 需要 diagnose 详细信息就超了。**SLA 应改成"30 秒内定位或给出 layer + top-3 可能原因"，而不是"30 秒定位"**。

### §6 反例清单　✅ **基本成立，可补漏**
现有 5 条正确。红方 Q7 的建议应加入：
- 不 halt CPU 期间读串口（会丢数据）
- 不同时开多个 UDP 连接到 :5001（trace_dump 并发互抢）
- 不在 hgfs 掉线时读固件（会读到空文件误判 make 失败）
- 不在 openocd 未 shutdown 时启动第二个 openocd（DAPLink busy）

### §7 判据表阈值　🟨→🟥 **多处存疑，部分证伪**
- **"L4 test pattern err <1%"**：与 hw_selftest.py:117 一致（`args.threshold` 默认 1.0，`div < 2.0`），有代码依据。✅
- **"TRACECLK 目标 ±5%"**：直接从 dbg_regfile 的 `TRACECLK_FREQ` 读——**但见下面 §9.5 的证伪**，边沿计数器在 100M+ 会欠采样，5% 阈值毫无意义（读数本身漂移可能 >30%）。**这一条在当前 RTL 上证伪。**
- **"L8 fsync ≥10/8KB、deframed ≥1KB/8KB、A-sync ≥5、INSTR_RANGE ≥100"**：这些阈值来源不明。8KB 抓样在 100M TRACECLK 下只覆盖 ~80µs，A-sync 周期是可配的（TRCSYNCPR 默认 2^10=1024 字节，8KB 大概有 8 个），所以"≥10 A-sync"在窗口太短时会 FAIL 正常态；BB-OFF 稀疏流下 deframed <1KB 也可能是正常的（BB-OFF 就是要稀疏）。**判据在不同配置下会误报 FAIL**。
- **"CoreMark 分数±5%"**：文档 §3.2 的 `.fw_expect.json` 例值来自 AGENT.md §4.1（150M→611, 200M→815, 300M→1223, 400M→1631），有实测依据。但**没考虑运行环境变量**（VM 时钟精度、DWT 校准、编译器 flags）——±5% 在极端 workload 差异下可能被击穿。
- **"3 次抓样 fsync 方差 <20%"、"3 次 CoreMark 方差 <0.5%"**：全是拍脑袋，无实测方差分布依据。

**判定**：阈值层混合了"有代码依据"和"拍脑袋"两类，工具会在拍脑袋阈值上产生误报。**必须先跑一次基线（已知全绿态）实测每个阈值的方差和边界**，把"3σ / P95" 作为判据依据，而不是脑内估计的百分数。

### §8 一句话（"跑之前先跑 trace_doctor"）　✅ 成立（口号）

### §9 dbg_regfile 集成　🟩 **动机+方向对，但技术细节有致命 bug；且遗漏 la_ddr_writer**
- **动机正确**：外部探测独立于 bit + 片上诊断秒级定位，是本项目"红方证伪链"经验的正确 crystallization。**这一节是全文最有价值的补充。**
- **诚实性加分**：明确承认"第一版没搜到 proposal 30，被用户点醒才补的"——这是 r25/r26/r27 一路批"选择性承认"之后的真正改进，值得肯定。
- **但依然有漏**（Q4）：
  - `la_ddr_writer.v` 的 `words_written` / `wr_lost_bytes` / `wr_ptr_words` 是**同样已实现的、正在被 proposal 32 黑匣子 top 使用的诊断寄存器**——文档没提。这些能给"DDR3 写侧是否溢出、缓存深度多少"的直接证据，是诊断 L4 相位 / L3 网络 / L7 溢出的重要交叉信号。**proposal 32 的黑匣子机制没进 §9 的"已实现资产"清单。**
  - **proposal 33 撤销的 per-lane IDELAY 校准**：文档提到 `--fix tap_scan`（clock IDELAY），但 per-lane data IDELAY 是 proposal 33 已实现过、后撤销、但代码可能还在的资产，可以给 L4 的更细粒度诊断（区分"clk 相位错" vs "某 lane skew"）。文档没提。
  - proposal 22 的"v0 golden check"——我 grep 没找到该术语的现有实现，蓝方（我）此前提到时可能是回忆错误；**如果它没实现，红方 Q4 这一条应诚实标注"proposal 22 未见 golden check 实现，无遗漏"**。这里应回填到 §9 说明。
- **诚实标注**：§9 分方案 A/B、明确列出 "clktap 主力 bit 未含 dbg_regfile" 的 gap，这一点做得对，比第一版好很多。

---

## §9.5 dbg_regfile 本身可信度 —— 🟥 **发现结构性 bug，足以否定 §9 强化 L4 的路径**

这是**本次评审的核心发现**。红方 Q5 命中要害。逐项核 `rtl/dbg_regfile.v`：

### 5.1 边沿检测方式（Q5 核心）—— 🟥 **100M TRACECLK 下必然欠采样**

`trace_mmcm_stream_top.v:392-397`：
```verilog
always @(posedge clk125) begin
    gclk_s0 <= raw_clk_ibuf;   gclk_s1 <= gclk_s0;  gclk_s2 <= gclk_s1;
    ...
end
wire gpio_clk_edge = gclk_s1 ^ gclk_s2;      // any edge on TRACECLK
```

**这是"clk125 每拍看电平变化"，不是真正的边沿检测。** 相当于用 125 MSPS 采样一个 TRACECLK 方波，然后靠"连续两拍 XOR"发现"电平变了"。这个方法的正确工作范围有个 Nyquist 界：**能可靠检出边沿的 TRACECLK 频率上限 ≈ clk125 / 2 = 62.5 MHz**（且要求边沿间距均匀）。

**在我们目标频段 100-112.5 MHz**（AGENT.md §4.1：150M sysclk→TRACECLK 112.5M；400M sysclk→TRACECLK 100M）：
- TRACECLK 100M：半周期 5 ns；clk125 周期 8 ns。**一个 clk125 周期内 TRACECLK 已经完成一次高低跳变**——`gclk_s1 ^ gclk_s2` 可能显示"两拍电平相同"（漏了一个完整脉冲），也可能显示"变了"（抓到某个边沿）。**每个 clk125 拍抓到边沿的概率取决于两个时钟的相位关系，理论平均命中率 ~50-80%，且高度依赖 aliasing 相位。**
- TRACECLK 112.5M：更差，一个 clk125 拍内 TRACECLK 完成 1.125 次跳变，欠采样更严重。

**后果**：
- **`TRACECLK_FREQ` 报值虚假**：文档说"edges / 33554.4 = MHz"。100M TRACECLK 每 16.777ms 应有 3.355M 个真实边沿，但 `gpio_clk_edge` 一个 clk125 拍最多产 1 个脉冲，一个 16.777ms 窗口 clk125 拍数 = 2.097M——**上限就是 2.097M**，即报回 **62.5 MHz**（clk125 / 2 的 Nyquist），而真值是 100M。**L4.b 判据 "TRACECLK_FREQ 固件 PLL 计算值±5%" 会把每次运行都报成 FAIL**——100M 真值 vs 62.5M 读回值差 37.5%，任何 ±5% 判据都击穿。
- **`GPIO_EDGES` 值可以是虚假的"引脚活跃"证据**：即使 TRACECLK 完全断掉，只要 raw 输入有毛刺或 IBUF 输出有抖动，`gclk_s1 ^ gclk_s2` 也可能产脉冲；反之，100M 干净 TRACECLK 也可能被欠采漏计。
- **`GAP_COUNT/GAP_MAX` 更不可靠**：定义"gap = 连续 GAP_TH=8 clk125 拍无边沿"（64 ns）。TRACECLK 停 64 ns 才判 gap——但欠采样本身就会造成"连续多拍无观察到边沿"的假象（尤其 aliasing 相位不利时），会把正常运行报成 GAP_COUNT 递增。

**这个 bug 直接摧毁 §9.5 §L4.a-c 的所有判据**。文档 §9.5 甚至说"读 `TRACECLK_FREQ` 与固件预期对比 ±5%"，读数本身就是错的。

### 5.2 sticky first_err 逻辑本身 —— ✅ **成立**
`first_code/first_time/first_ctx/have_first` 的锁存逻辑（第 87-104 行）正确：`cur_any && !have_first` 触发一次锁存后不再更新，符合 sticky 语义；`rst || clr` 清除。**这条逻辑本身可信**，Q5 对它的质疑不成立——诚实标注：sticky 部分没有 corner-case bug。

**但**：sticky 依赖于**输入 pulse 是否真的到达 dbg_regfile**。清单里 `e_mmcm_unlock`、`e_cap_overflow`、`e_selftx_stuck` 这些 pulse 在 trace_mmcm_stream_top 里的产生逻辑本身没审——如果上游 pulse 生成有 bug 或者是 level 而不是 pulse，dbg_regfile 也没办法。这不是 dbg_regfile 的问题，但 trace_doctor 把它当"绝对真理"时需注意。

### 5.3 数据依赖 `traceclk_active_q & ~traceclk_active` 生成 `e_no_traceclk`
`trace_mmcm_stream_top.v:380-381`：`e_no_traceclk = traceclk_active_q & ~traceclk_active`——**下降沿检测（active→inactive）**。这本身逻辑对，但 `traceclk_active` 的生成如果依赖 5.1 那种欠采边沿计数器（很可能是），那"active"判定本身在 100M+ 下就漂移了。**递归性 bug**，且指向同一个根因（边沿检测机制在 100M 下失效）。

### 5.4 判定
**dbg_regfile 的 sticky/counter/寄存器 map 部分可信；边沿检测/频率计/gap 检测部分在目标频段（100M+ TRACECLK）不可信。** §9.5 §L4.b-c 判据必须**先修 RTL 才能用**；§L4.a `GPIO_EDGES` 用作"引脚在翻转"的定性证据在阈值放宽到 "非零"时**弱可信**（因为即便欠采，正常 100M 运行也会产大量非零脉冲，能与"完全断"区分开），但**不能定量**。

---

## §9.6 主力 clktap bit 补 dbg_regfile 的重综合方案 —— 🟨 **过于乐观**

Q6 命中。方案 A "把 dbg_regfile 接进 `trace_stream_top.v`，工作量小几百 LUT+两条 wire"低估了两处风险：

1. **时序影响**：clktap bit 的采集眼是精心调过的（doc 16 记 100-112.5M 突破，clock IDELAY tap 值对时序敏感）。加入 dbg_regfile 会引入 clk125 域的新 fanout（读端口 mux）、新计数器（edge counters、gap detector、freq meter），Vivado 布线会重新收敛——**tap 值几乎肯定需要重扫**（doc 16 里每次综合后都要重扫 tap 是既有事实，这次不会更好）。
2. **仿真中的 `gclk_s1^gclk_s2` 会加进主力 bit**：以为片上诊断"顺便加一下"，实际上是把 §9.5 定位的**同一个错误的边沿检测机制**灌进主力 bit——**你会在主力 bit 上得到同样的错误频率读数**，然后 trace_doctor 会自信地报 FAIL。方案 A 的动机是"让主力 bit 有诊断"，但**当前 dbg_regfile 在 100M+ 频段是不可信的诊断**——先修边沿检测，再考虑集成到主力 bit。

**修复动作（root）**：改 `gpio_clk_edge` 生成方式为**下面二选一**：
- **A**：把 `raw_clk_ibuf` 直接送 IDDR 或用 clktap MMCM 的高频时钟（clk_data，200-450 MHz）采样后再降频到 clk125 域计数。**MMCM 已经存在**，只是当前 dbg_regfile 走的是 clk125 domain。
- **B**：用一个跨越两个时钟域的边沿计数器——TRACECLK 域的 free-running toggle-flip-flop（1 bit 状态每个 TRACECLK 上升沿翻转），clk125 域两次同步 + XOR 得到"toggle 事件"。计数上界仍是 clk125 / 2，但**准确性变好**（1 个 toggle = 1 个 TRACECLK 上升沿，而不是"两拍电平变了"）。**这种方式仍需要 Nyquist**，仍不能测 100M TRACECLK 的真实频率——如果要测真频率，必须 A。

方案 B 依然做 "GPIO 是否翻转" 的定性判据可以（比 XOR-level 更可靠），但**准确频率读数只能用 A**。

### §9.6 判定
**方案 A 的正确形式**是"重综合含**修好的**边沿检测 + dbg_regfile 的 clktap bit"，不是原样加一个坏的边沿检测。工作量比蓝方估的大 —— **重扫 clock IDELAY tap** + **RTL 改边沿检测机制** + **验证与主 clktap 时序不冲突** = 至少 0.5-1 天。方案 B（tool 兜底）应作为**主线**，等 RTL 修好再切 A。

---

## Q8 工作量　🟨 **P0 低估、P0.5 严重低估**
- P0 一天：实际 1.5-2 天（见 §5 判定）。
- P0.5 clktap+dbg 重综合：一天，蓝方没显式列 P0.5 独立工时——AGENT.md 显示每次重综合都要重扫 tap（复杂度高，动辄半天）。
- P1 半天：合理。
- P2 一天：合理，但 `--fix tap_scan` 内部至少要 60s（32 tap × 抓样评分 + openocd 交互）。

**总工期**：至少 3-4 天（含 clktap+dbg 重综合与 tap 重扫），文档写"1+0.5+1=2.5 天" 偏乐观 30-40%。

---

## Q9 诚实性　✅ **喜大于忧**
- **喜**：主动承认"第一版漏了 proposal 30 是被用户点醒"（本项目 r25-r30 一路批"选择性承认"之后的真正改进）、诚实标注方案 A/B 二选一、承认阈值需要实测校准。
- **忧**：
  - "30 秒定位"这个 SLA **没有实测依据**（Q8 已证约需 20-40s，FAIL 时超）。
  - §2 早停照搬 shift-left 但项目内**没验证过它对隐藏耦合失效模式（proposal 38 类型）的适用性**——这是本项目老毛病"未验证假设当结论"的隐性复发（虽然比 proposal 34 轻）。
  - §9 遗漏 proposal 32 la_ddr_writer 和 proposal 33 per-lane IDELAY 资产。
  - §9.4/§9.5 把 dbg_regfile 当"已实现即可信"，没审边沿检测机制——虽然文档确实标了这是"方案 A 建议+B 兜底"，但没有"dbg_regfile 本身可能有 bug"这一层怀疑。**这条恰恰是本项目老毛病的另一面：把"已实现"当"已验证"**，是 proposal 38 mem 基址那种 bug 的重现路径。

---

## 整篇裁决

**不能直接进入 P0 实现。必须先补以下三条：**

1. **修 RTL 边沿检测（阻断）**：`gpio_clk_edge` 改为 A/B 二选一（首选 A，MMCM 域采样后降频）。这是"trace_doctor 强诊断路径"的地基。
2. **§7 判据表实测校准（阻断）**：先跑一次已知全绿态基线，测每个数值判据的方差分布，把"±5% / <20% / <0.5%" 换成 3σ 或 P95 数据。
3. **§2 早停模型加分支（阻断）**：加一条"全绿但主诉失败 → 强制 --continue-on-fail + 深度模式"回路。

**可以并行做的（不阻断）**：P0 外部探测层（L0-L4 除 L4.b 频率外）实现、`.bit_db.json`/`.fw_expect.json` 白名单落地、AGENT.md 已列坑点补进 §1、`--dump-all` bundle 实现。

**替代方案（根本性）**：
如果不想改 RTL，**trace_doctor 应把 dbg_regfile 频率计从判据中降级为"参考读数"**——只用 `first_code / first_ctx / GPIO_EDGES 非零/为零`（定性）这些**不依赖精确边沿计数的字段**，绕开欠采样问题。这条兜底路可以让 P0 立刻上线，代价是 §9.5 §L4.b/c 数值判据被移除，只保留 L4.a（gpio 引脚是否翻转，非零/为零二值）和 L4.d（FIRST_ERR_CODE 查表）。

---

## §1-9 判定表

| # | 节 | 判定 | 关键理由 |
|---|---|---|---|
| §1 | 失效模式清单 | 🟨存疑 | 仍有 3-5 条实际踩过的失效模式缺席 |
| §2 | 层间依赖 + 早停 | 🟨存疑 | 早停对隐藏耦合（proposal 38 类型）失效，需加深度分支 |
| §3 | CLI + 组件 | ✅成立 | 白名单版本化流程需补 |
| §4 | 补漏（温度/版本/交叉验证）| ✅成立且是加分 | 但阈值来源部分不明 |
| §5 | 分阶段 | 🟨低估 | P0 至少 1.5-2 天 |
| §6 | 反例清单 | ✅基本成立 | 补 4 条明确边界 |
| §7 | 判据表 | 🟥部分证伪 | TRACECLK±5% 判据因 §9.5 bug 失效；多阈值拍脑袋 |
| §8 | 口号 | ✅成立 |  |
| §9.1-§9.4 | dbg_regfile 集成方向 | ✅方向对 | 但漏 proposal 32 la_ddr_writer 资产 |
| §9.5 | dbg_regfile 本身可信度 | 🟥**结构性 bug** | 100M+ 频段边沿检测欠采样，频率计不可信 |
| §9.6 | 方案 A（重综合）| 🟨过于乐观 | 时序风险 + tap 重扫 + 灌入坏边沿检测 |
| 总 | 一键诊断工具 | 🟨方向对，不能直接进 P0 | 先修 RTL、校准阈值、加早停深度分支 |

---

## 一句话给用户

**方向和动机都对，§9 引入 dbg_regfile 是本项目历次评审后最有价值的一次修补，主动承认漏了 proposal 30 是真诚——但工具的地基有一个结构性 bug 你需要先修：dbg_regfile 里那句 `gpio_clk_edge = gclk_s1 ^ gclk_s2` 是用 clk125（125 MHz）两拍电平 XOR 当"边沿检测"，Nyquist 上限只有 62.5 MHz，而你们主力目标 TRACECLK 是 100-112.5 MHz，边沿会稳定性地漏计 30-40%，`TRACECLK_FREQ` 报回值上限就是 62.5MHz、真值 100M 的读数是错的，§9.5 §L4.b 判据"TRACECLK 目标±5%"用这个读数会把每次正常运行都误报 FAIL。除此之外，§7 里"3 次抓样 fsync 方差 <20%"这种阈值全是拍脑袋没有基线实测支撑、§2 早停对 proposal 38 那种"逐层绿但整链错"的隐藏耦合失效会掩盖真凶、§9 遗漏了 proposal 32 的 la_ddr_writer 诊断资产、方案 A（重综合含 dbg_regfile 的 clktap bit）会灌入同一个错的边沿检测机制、"30 秒定位"这个 SLA 从没实测过。P0 先不要动 dbg_regfile 的边沿判据，只保留 `FIRST_ERR_CODE`（查表）和 `GPIO_EDGES 非零/零`（二值定性）这些不依赖精确计数的字段；同时并行修 RTL 把边沿检测挪到 clktap MMCM 时钟域采样后再降频到 clk125 计数，重扫一次 tap；再回来做 §9.5 强诊断路径。**
